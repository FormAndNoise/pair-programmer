"""Non-blocking terminal keyboard controller for live operator steering and inspection."""

from __future__ import annotations

import atexit
import codecs
import os
import signal
import sys
import threading
import time
from typing import Any, Callable, Optional

from agent_core.events import AgentEvent, EventBus, EventType
from agile.pair_engine import PairProgrammingEngine
from sandbox.runner import SandboxRunner
from tui.app import PairProgrammingTUI


MAX_STEERING_CHARS = 1000
ESC_SEQUENCE_TIMEOUT = 0.1
_POSIX_KEY_DECODER = codecs.getincrementaldecoder("utf-8")("ignore")


def _default_nonblocking_key_reader() -> Optional[str]:
    """Read a single keypress from the terminal without blocking, or return None."""
    if not sys.stdin or not sys.stdin.isatty():
        return None

    if os.name == "nt":
        try:
            import msvcrt

            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                # Ignore prefix bytes for special function/arrow keys
                if ch in ("\x00", "\xe0"):
                    if msvcrt.kbhit():
                        msvcrt.getwch()
                    return None
                return ch
        except Exception:
            return None
        return None

    try:
        import select

        fd = sys.stdin.fileno()
        rlist, _, _ = select.select([fd], [], [], 0.0)
        if rlist:
            # Read the same file descriptor checked by select. Buffered reads can
            # otherwise consume extra bytes and leave select out of sync.
            data = os.read(fd, 1)
            return _POSIX_KEY_DECODER.decode(data, final=False) or None
    except Exception:
        return None
    return None


class TerminalKeyboardController:
    """Polls terminal keystrokes to pause, steer, inspect diffs, or stop a running sprint."""

    def __init__(
        self,
        engine: PairProgrammingEngine,
        bus: EventBus,
        tui: Optional[PairProgrammingTUI] = None,
        runner: Optional[SandboxRunner] = None,
        git_manager: Optional[Any] = None,
        key_reader: Optional[Callable[[], Optional[str]]] = None,
        poll_interval: float = 0.05,
    ) -> None:
        self.engine = engine
        self.bus = bus
        self.tui = tui
        self.runner = runner
        self.git_manager = git_manager
        self.key_reader = key_reader or _default_nonblocking_key_reader
        self.poll_interval = poll_interval

        self._steering_buffer: str = ""
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._old_term_settings: Optional[Any] = None
        self._stdin_fd: Optional[int] = None
        self._signal_handlers: dict[int, Any] = {}
        self._atexit_registered = False
        self._escape_state: Optional[str] = None
        self._escape_started_at: Optional[float] = None
        self._lifecycle_lock = threading.RLock()

    @property
    def steering_buffer(self) -> str:
        """Return the current operator steering input buffer."""
        return self._steering_buffer

    def start(self) -> None:
        """Start the background keyboard polling thread."""
        with self._lifecycle_lock:
            if self._running:
                return
            if os.name != "nt":
                try:
                    import termios
                    import tty

                    if sys.stdin.isatty():
                        self._stdin_fd = sys.stdin.fileno()
                        self._old_term_settings = termios.tcgetattr(self._stdin_fd)
                        tty.setcbreak(self._stdin_fd)
                except Exception:
                    self._old_term_settings = None
                    self._stdin_fd = None

            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._poll_loop,
                name="PairProgrammerKeyboardController",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception:
                self._running = False
                self._stop_event.set()
                self._restore_terminal()
                self._thread = None
                raise

            if not self._atexit_registered:
                atexit.register(self.stop)
                self._atexit_registered = True
            self._install_signal_handlers()

    def stop(self) -> None:
        """Stop the background keyboard polling thread."""
        with self._lifecycle_lock:
            self._running = False
            self._stop_event.set()
            thread = self._thread

        if thread is not None and thread is not threading.current_thread():
            if thread.is_alive():
                # The default reader is non-blocking, so wait until polling exits
                # before restoring terminal state.
                thread.join()

        with self._lifecycle_lock:
            if self._thread is thread:
                self._thread = None
            self._restore_terminal()
            self._restore_signal_handlers()
            if self._atexit_registered:
                atexit.unregister(self.stop)
                self._atexit_registered = False

    def _restore_terminal(self) -> None:
        """Restore saved POSIX terminal settings when this controller changed them."""
        if self._old_term_settings is None or self._stdin_fd is None:
            return
        try:
            import termios

            termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._old_term_settings)
        except Exception:
            pass
        finally:
            self._old_term_settings = None
            self._stdin_fd = None

    def _install_signal_handlers(self) -> None:
        """Register cleanup handlers when start runs on the main thread."""
        if threading.current_thread() is not threading.main_thread():
            return
        for signum in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if signum is None or signum in self._signal_handlers:
                continue
            try:
                previous = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
                self._signal_handlers[signum] = previous
            except (OSError, ValueError):
                continue

    def _restore_signal_handlers(self) -> None:
        """Restore prior handlers from the main thread."""
        if threading.current_thread() is not threading.main_thread():
            return
        for signum, previous in list(self._signal_handlers.items()):
            try:
                signal.signal(signum, previous)
            except (OSError, ValueError):
                continue
            self._signal_handlers.pop(signum, None)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Restore the terminal, then preserve the signal's previous behavior."""
        previous = self._signal_handlers.get(signum, signal.SIG_DFL)
        self.stop()
        if previous == signal.SIG_IGN:
            return
        if callable(previous):
            previous(signum, frame)
            return
        if previous == signal.SIG_DFL:
            # stop() restored SIG_DFL, so redeliver the signal with its normal action.
            os.kill(os.getpid(), signum)

    def __enter__(self) -> TerminalKeyboardController:
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def submit_steering(self, guidance: str, resume_after: bool = True) -> None:
        """Inject operator steering guidance directly onto the EventBus."""
        clean = guidance.strip()[:MAX_STEERING_CHARS]
        if clean:
            self.bus.publish(
                AgentEvent(
                    event_type=EventType.OPERATOR_STEER,
                    payload={"guidance": clean},
                )
            )
        self._set_buffer("")
        if resume_after and self.engine.is_paused:
            self.engine.resume()

    def handle_key(self, key: str) -> str:
        """Process a single key event and return the resulting action label."""
        if not key:
            return "ignored"

        if self._consume_escape_input(key):
            return "ignored"

        # When paused, capture inline steering text or resume on Enter/Space.
        if self.engine.is_paused:
            if key in ("\r", "\n"):
                if self._steering_buffer.strip():
                    self.submit_steering(self._steering_buffer, resume_after=True)
                    return "steered"
                self._set_buffer("")
                self.engine.resume()
                return "resumed"

            if key == " " and not self._steering_buffer:
                self.engine.resume()
                return "resumed"

            if key in ("\x08", "\x7f"):
                self._set_buffer(self._steering_buffer[:-1])
                return "typing"

            if len(key) == 1 and key.isprintable():
                self._set_buffer(self._steering_buffer + key)
                return "typing"

            return "ignored"

        # Normal running controls
        if key == " ":
            self._set_buffer("")
            self.engine.pause()
            return "paused"

        if key in ("d", "D"):
            self._inspect_diff()
            return "diff"

        if key in ("v", "V"):
            self._inspect_terminal()
            return "terminal"

        if key in ("q", "Q"):
            self.engine.request_stop()
            return "quit"

        return "ignored"

    def _set_buffer(self, value: str) -> None:
        self._steering_buffer = value[:MAX_STEERING_CHARS]
        if self.tui is not None:
            self.tui.steering_buffer = self._steering_buffer

    def _consume_escape_input(self, key: str) -> bool:
        """Discard ESC, CSI, and OSC input so terminal keys never steer the sprint."""
        if key.startswith("\x1b"):
            if self._escape_state == "osc":
                self._escape_state = "osc_escape"
            else:
                self._escape_state = None if len(key) > 1 else "escape"
                self._escape_started_at = time.monotonic() if len(key) == 1 else None
            return True

        state = self._escape_state
        if state is None:
            return False
        if (
            state == "escape"
            and self._escape_started_at is not None
            and time.monotonic() - self._escape_started_at > ESC_SEQUENCE_TIMEOUT
            and key not in ("[", "]")
        ):
            self._escape_state = None
            self._escape_started_at = None
            return False
        if state == "escape":
            if key == "[":
                self._escape_state = "csi"
                self._escape_started_at = None
            elif key == "]":
                self._escape_state = "osc"
                self._escape_started_at = None
            else:
                self._escape_state = None
                self._escape_started_at = None
            return True
        if state == "csi":
            if key and "@" <= key[-1] <= "~":
                self._escape_state = None
                self._escape_started_at = None
            return True
        if state == "osc":
            if "\x07" in key:
                self._escape_state = None
                self._escape_started_at = None
            elif "\x1b" in key:
                self._escape_state = "osc_escape"
            return True
        if state == "osc_escape":
            self._escape_state = None if key == "\\" else "osc"
            self._escape_started_at = None
            return True
        self._escape_state = None
        self._escape_started_at = None
        return True

    def _inspect_diff(self) -> None:
        """Emit a workspace diff or file status summary to the TUI VM log."""
        summary = "No workspace diff available."
        try:
            if self.git_manager is not None:
                stat = self.git_manager._git("status", "--short").strip()
                summary = f"Git status: {stat.splitlines()[0]}" if stat else "Git working tree is clean."
            elif self.runner is not None:
                files = [p.name for p in self.runner.workspace_dir.iterdir() if p.is_file()]
                summary = f"Workspace files ({len(files)}): {', '.join(sorted(files)[:6])}" if files else "Workspace is empty."
        except Exception as exc:
            summary = f"Diff inspection error: {exc}"

        self.bus.publish(
            AgentEvent(
                event_type=EventType.TOOL_RESULT,
                payload={"tool_name": "diff_inspect", "output": summary, "is_error": False},
            )
        )

    def _inspect_terminal(self) -> None:
        """Emit active sandbox and story status to the TUI VM log."""
        story = self.engine.backlog_mgr.sprint.active_story
        story_label = f"{story.id} ({story.status.value})" if story else "idle"
        msg = f"Active story: {story_label} | Sandbox: {self.runner.workspace_dir.name if self.runner else 'n/a'}"
        self.bus.publish(
            AgentEvent(
                event_type=EventType.TOOL_RESULT,
                payload={"tool_name": "terminal_view", "output": msg, "is_error": False},
            )
        )

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set() and not self.engine.stop_requested:
            try:
                key = self.key_reader()
                if key is not None:
                    self.handle_key(key)
            except Exception:
                pass
            self._stop_event.wait(self.poll_interval)
        self._running = False
