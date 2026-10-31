"""Non-blocking terminal keyboard controller for live operator steering and inspection."""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Callable, Optional

from agent_core.events import AgentEvent, EventBus, EventType
from agile.pair_engine import PairProgrammingEngine
from sandbox.runner import SandboxRunner
from tui.app import PairProgrammingTUI


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

        rlist, _, _ = select.select([sys.stdin], [], [], 0.0)
        if rlist:
            return sys.stdin.read(1)
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
        self._thread: Optional[threading.Thread] = None

    @property
    def steering_buffer(self) -> str:
        """Return the current operator steering input buffer."""
        return self._steering_buffer

    def start(self) -> None:
        """Start the background keyboard polling thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="PairProgrammerKeyboardController",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the background keyboard polling thread."""
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None

    def __enter__(self) -> TerminalKeyboardController:
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def submit_steering(self, guidance: str, resume_after: bool = True) -> None:
        """Inject operator steering guidance directly onto the EventBus."""
        clean = guidance.strip()
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

        # When paused, capture inline steering text or resume on Enter/Space/Esc
        if self.engine.is_paused:
            if key in ("\r", "\n"):
                if self._steering_buffer.strip():
                    self.submit_steering(self._steering_buffer, resume_after=True)
                    return "steered"
                self._set_buffer("")
                self.engine.resume()
                return "resumed"

            if key == "\x1b":
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
        self._steering_buffer = value
        if self.tui is not None:
            self.tui.steering_buffer = value

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
        while self._running and not self.engine.stop_requested:
            try:
                key = self.key_reader()
                if key is not None:
                    self.handle_key(key)
            except Exception:
                pass
            time.sleep(self.poll_interval)
