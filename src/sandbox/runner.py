"""Contained workspace runner with path confinement and execution watchdogs."""

from __future__ import annotations

import ctypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from pydantic import BaseModel
from agent_core.sanitize import strip_ansi


class ExecutionResult(BaseModel):
    """Result of a command executed in the sandbox."""
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_ms: float = 0.0

    @property
    def is_success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def format_output(self) -> str:
        """Format a clear human-and-model-readable execution summary."""
        parts = []
        if self.timed_out:
            parts.append("[TIMEOUT] Command exceeded time limit and was terminated.")
        elif self.exit_code != 0:
            parts.append(f"[EXIT CODE: {self.exit_code}]")

        if self.stdout.strip():
            parts.append("STDOUT:\n" + self.stdout.strip())
        if self.stderr.strip():
            parts.append("STDERR:\n" + self.stderr.strip())

        if not parts:
            return "[No output produced]"
        return "\n\n".join(parts)


_SENSITIVE_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AZURE_OPENAI_API_KEY",
    "GROQ_API_KEY",
    "COHERE_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "HF_TOKEN",
    "GITHUB_TOKEN",
    "GITLAB_TOKEN",
    "AWS_SESSION_TOKEN",
    "DATABASE_URL",
    "PAIR_PROGRAMMER_TOKEN",
}

_LOADER_ENV_KEYS = {
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONHOME",
    "PYTHONINSPECT",
    "PYTHONEXECUTABLE",
    "NODE_OPTIONS",
    "RUBYOPT",
    "PERL5OPT",
    "JAVA_TOOL_OPTIONS",
    "BASH_ENV",
    "ENV",
    "PROMPT_COMMAND",
}

_BLOCKED_ENV_PREFIXES = (
    "ANTHROPIC_",
    "OPENAI_",
    "GEMINI_",
    "GOOGLE_",
    "OPENROUTER_",
    "GROQ_",
    "COHERE_",
    "MISTRAL_",
    "DEEPSEEK_",
    "HF_",
    "HUGGINGFACE_",
    "AWS_",
    "AZURE_",
    "GCP_",
    "SLACK_",
    "GITHUB_",
    "GITLAB_",
    "GIT_",
    "SSH_",
    "DOCKER_",
    "KUBE",
    "PAIR_PROGRAMMER_",
    "LD_",
    "DYLD_",
)

_BLOCKED_ENV_EXACT = {
    "EDITOR",
    "VISUAL",
    "BROWSER",
    "DISPLAY",
    "ASKPASS",
    "SSH_ASKPASS",
    "GIT_ASKPASS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "FTP_PROXY",
}

_ALLOWED_HOST_ENV_KEYS_COMMON = {
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TERM",
    "TZ",
}

_ALLOWED_HOST_ENV_KEYS_WINDOWS = {
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "OS",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "SYSTEMDRIVE",
}

_PROTECTED_SYSTEM_ENV_KEYS = {
    "PATH",
    "HOME",
    "USERPROFILE",
    "TMP",
    "TEMP",
    "TMPDIR",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "SYSTEMDRIVE",
}


def validate_workspace_path(workspace_dir: str | Path) -> Path:
    """Resolve and validate that workspace_dir is not a filesystem root or user home directory."""
    resolved = Path(workspace_dir).resolve()
    if resolved.parent == resolved:
        raise ValueError(
            f"Refusing unsafe sandbox workspace '{resolved}': cannot use filesystem root."
        )
    try:
        home_dir = Path.home().resolve()
        if resolved == home_dir:
            raise ValueError(
                f"Refusing unsafe sandbox workspace '{resolved}': cannot use user home directory."
            )
    except RuntimeError:
        pass
    return resolved


def _scrub_environment(
    env: os._Environ | Dict[str, str],
    *,
    trusted_loader_keys: frozenset[str] = frozenset(),
    reject_path: bool = False,
) -> Dict[str, str]:
    """Remove secrets, provider configs, git/ssh hooks, and loader overrides from a mapping."""
    clean_env = dict(env)
    for k in list(clean_env.keys()):
        upper_k = k.upper()
        if upper_k in trusted_loader_keys:
            continue
        if (
            upper_k in _SENSITIVE_ENV_KEYS
            or upper_k in _LOADER_ENV_KEYS
            or upper_k in _BLOCKED_ENV_EXACT
            or upper_k.startswith(_BLOCKED_ENV_PREFIXES)
            or upper_k.endswith(
                (
                    "_API_KEY",
                    "_SECRET",
                    "_TOKEN",
                    "_SECRET_KEY",
                    "_PASSWORD",
                    "_DSN",
                    "_PRIVATE_KEY",
                    "_KEY",
                    "_CREDENTIALS",
                    "_PROXY",
                )
            )
            or "_AUTH" in upper_k
            or "_PASS" in upper_k
            or "_SECRET" in upper_k
            or "_TOKEN" in upper_k
            or (reject_path and upper_k in _PROTECTED_SYSTEM_ENV_KEYS)
        ):
            del clean_env[k]
    return clean_env


def build_sanitized_environment(
    base_env: Optional[os._Environ | Dict[str, str]] = None,
    *,
    workspace_dir: Optional[Path] = None,
    extra_env: Optional[Dict[str, str]] = None,
    trusted_env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Construct a minimal platform-aware allowlisted environment for child processes."""
    source = os.environ if base_env is None else base_env
    allowed_keys = set(_ALLOWED_HOST_ENV_KEYS_COMMON)
    if os.name == "nt":
        allowed_keys |= _ALLOWED_HOST_ENV_KEYS_WINDOWS

    env: Dict[str, str] = {}
    for k, v in source.items():
        if k.upper() in allowed_keys:
            env[k] = v

    if workspace_dir is not None:
        ws_str = str(workspace_dir)
        tmp_dir = workspace_dir / ".tmp"
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_str = str(tmp_dir)
        except OSError:
            tmp_str = ws_str
        env["HOME"] = ws_str
        env["TMPDIR"] = tmp_str
        env["TEMP"] = tmp_str
        env["TMP"] = tmp_str

    env["PAGER"] = "cat"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"

    trusted_loader_keys = frozenset((trusted_env or {}).keys()) & frozenset({"PYTHONPATH"})
    if extra_env:
        safe_extra = _scrub_environment(
            extra_env,
            trusted_loader_keys=frozenset(),
            reject_path=True,
        )
        env.update(safe_extra)

    env = _scrub_environment(env, trusted_loader_keys=trusted_loader_keys, reject_path=False)

    if trusted_env:
        safe_trusted = _scrub_environment(
            trusted_env,
            trusted_loader_keys=trusted_loader_keys,
            reject_path=True,
        )
        env.update(safe_trusted)

    return env


def sanitize_and_truncate_output(text: str, max_output_chars: int) -> str:
    """Strip terminal controls and truncate excessive output strings."""
    text = strip_ansi(text)
    if len(text) <= max_output_chars:
        return text

    omitted_count = len(text) - max_output_chars
    head_chars = max_output_chars // 2
    tail_chars = max_output_chars // 2

    return (
        text[:head_chars]
        + f"\n\n... [TRUNCATED: {omitted_count} characters omitted to protect context window] ...\n\n"
        + text[-tail_chars:]
    )


def _create_windows_job_object() -> Optional[int]:
    """Create a Windows Job Object configured with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE."""
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        info.BasicLimitInformation.LimitFlags = 0x2000
        JobObjectExtendedLimitInformation = 9
        res = kernel32.SetInformationJobObject(
            ctypes.c_void_p(job),
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not res:
            kernel32.CloseHandle(ctypes.c_void_p(job))
            return None
        return int(job)
    except Exception:
        return None


def _assign_proc_to_windows_job(job: Optional[int], pid: int) -> None:
    if os.name != "nt" or not job:
        return
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        kernel32.OpenProcess.restype = ctypes.c_void_p
        h_proc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if h_proc:
            try:
                kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(h_proc))
            finally:
                kernel32.CloseHandle(ctypes.c_void_p(h_proc))
    except Exception:
        pass


def _close_windows_job(job: Optional[int]) -> None:
    if os.name != "nt" or not job:
        return
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.TerminateJobObject(ctypes.c_void_p(job), 1)
        kernel32.CloseHandle(ctypes.c_void_p(job))
    except Exception:
        pass


def _run_bounded_subprocess(
    argv: List[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 60,
    max_capture_bytes: int = 65536,
    hard_kill_bytes: int = 2_000_000,
    kill_callback: Optional[Callable[[subprocess.Popen], None]] = None,
) -> Tuple[int, str, str, bool]:
    """Execute a subprocess with DEVNULL stdin, job containment, and bounded pipe readers.

    Prevents unbounded memory growth from output floods (e.g. ``yes``) while
    preserving head and tail bytes so normal truncation banners remain accurate.
    Returns ``(exit_code, stdout_str, stderr_str, timed_out)``.
    """
    job_handle = _create_windows_job_object()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=(os.name != "nt"),
    )
    _assign_proc_to_windows_job(job_handle, proc.pid)

    half_cap = max(2048, max_capture_bytes // 2)

    def _terminate_all() -> None:
        _close_windows_job(job_handle)
        if kill_callback is not None:
            kill_callback(proc)

    def _drain_pipe(pipe, bucket: dict) -> None:
        head = bytearray()
        tail = bytearray()
        total_bytes = 0
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if len(head) < half_cap:
                    need = half_cap - len(head)
                    head.extend(chunk[:need])
                    remainder = chunk[need:]
                else:
                    remainder = chunk

                if remainder:
                    tail.extend(remainder)
                    if len(tail) > half_cap:
                        del tail[: len(tail) - half_cap]

                if total_bytes >= hard_kill_bytes:
                    bucket["flood_killed"] = True
                    _terminate_all()
                    break
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass
        bucket["head"] = bytes(head)
        bucket["tail"] = bytes(tail)
        bucket["total"] = total_bytes

    out_bucket: dict = {"head": b"", "tail": b"", "total": 0, "flood_killed": False}
    err_bucket: dict = {"head": b"", "tail": b"", "total": 0, "flood_killed": False}

    t_out = threading.Thread(target=_drain_pipe, args=(proc.stdout, out_bucket), daemon=True)
    t_err = threading.Thread(target=_drain_pipe, args=(proc.stderr, err_bucket), daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_all()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    finally:
        # Ensure any background descendants holding pipe handles are terminated
        if timed_out or out_bucket.get("flood_killed") or err_bucket.get("flood_killed"):
            _terminate_all()
        else:
            # Give reader threads a brief moment to finish normal EOF drain before closing job
            t_out.join(timeout=0.5)
            t_err.join(timeout=0.5)
            if t_out.is_alive() or t_err.is_alive():
                _terminate_all()
            else:
                _close_windows_job(job_handle)

        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    pass

    t_out.join(timeout=2)
    t_err.join(timeout=2)

    def _assemble(bucket: dict) -> str:
        head_str = bucket["head"].decode("utf-8", errors="replace")
        tail_str = bucket["tail"].decode("utf-8", errors="replace")
        omitted = max(0, bucket["total"] - len(bucket["head"]) - len(bucket["tail"]))
        if omitted > 0:
            # Fill middle with placeholder so character-count truncation remains accurate
            filler = "X" * min(omitted, max_capture_bytes)
            return head_str + filler + tail_str
        return head_str + tail_str

    stdout_str = _assemble(out_bucket)
    stderr_str = _assemble(err_bucket)
    exit_code = -1 if timed_out else (proc.returncode if proc.returncode is not None else -1)
    return exit_code, stdout_str, stderr_str, timed_out


class SandboxRunner:
    """Manages workspace file isolation and subprocess execution."""

    def __init__(
        self,
        workspace_dir: str | Path,
        default_timeout_seconds: int = 60,
        max_output_chars: int = 8000,
    ) -> None:
        self.workspace_dir = validate_workspace_path(workspace_dir)
        self.default_timeout = max(1, min(600, int(default_timeout_seconds)))
        self.max_output_chars = max(100, min(100_000, int(max_output_chars)))

        # Ensure sandbox directory exists
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def resolve_path(self, target_path: str | Path) -> Path:
        """Resolve a path and ensure it cannot escape the workspace root.

        Raises PermissionError if the path attempts traversal outside workspace.
        """
        raw_path = Path(target_path)
        if raw_path.is_absolute():
            candidate = raw_path
            resolved = raw_path.resolve()
        else:
            candidate = self.workspace_dir / raw_path
            resolved = candidate.resolve()

        # Confinement check on resolved path
        try:
            resolved.relative_to(self.workspace_dir)
        except ValueError:
            raise PermissionError(
                f"Path traversal detected: '{target_path}' resolves to '{resolved}', "
                f"which is outside sandbox workspace '{self.workspace_dir}'."
            )

        # Reject symlinks whose target escapes the workspace
        try:
            if candidate.is_symlink():
                link_target = candidate.resolve()
                link_target.relative_to(self.workspace_dir)
        except ValueError:
            raise PermissionError(
                f"Symlink escape detected: '{target_path}' points outside sandbox workspace."
            )

        return resolved

    def _kill_process_tree(self, proc: subprocess.Popen) -> None:
        """Terminate process and all child descendants."""
        if proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass
        else:
            try:
                import signal
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
        try:
            proc.kill()
        except Exception:
            pass

    @staticmethod
    def _parse_command_argv(command: str) -> List[str]:
        """Split a command string into an argv list without invoking a shell.

        Preserves Windows backslash paths (e.g. ``C:\\dir\\file.py``) while still
        supporting standard POSIX single/double quoting and escaped quotes.
        """
        if not command or not command.strip():
            raise ValueError("Command cannot be empty.")
        # Preserve backslashes that are not escaping quotes or another backslash
        normalized = re.sub(r'\\(?![\\"\'])', r"\\\\", command)
        argv = shlex.split(normalized, posix=True)
        if not argv:
            raise ValueError("Command cannot be empty.")
        # Normalize python/python3 executable to current interpreter when available
        if argv[0] in ("python", "python3") and sys.executable:
            argv[0] = sys.executable
        return argv

    def run_command(
        self,
        command: str,
        timeout_seconds: Optional[int] = None,
        extra_env: Optional[Dict[str, str]] = None,
        *,
        trusted_env: Optional[Dict[str, str]] = None,
    ) -> ExecutionResult:
        """Run a command via direct argv execution with timeout protection inside the workspace.

        Use ``trusted_env`` only for harness-owned values needed by DoD checks.
        The model-facing tool interface cannot set this environment mapping.
        """
        if timeout_seconds is not None and timeout_seconds <= 0:
            return ExecutionResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr="Error: timeout_seconds must be greater than zero.",
                timed_out=False,
                duration_ms=0.0,
            )

        timeout = max(
            1,
            min(600, int(timeout_seconds if timeout_seconds is not None else self.default_timeout)),
        )
        start_time = time.perf_counter()

        try:
            argv = self._parse_command_argv(command)
        except ValueError as e:
            return ExecutionResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr=f"Invalid command syntax: {e}",
                timed_out=False,
                duration_ms=0.0,
            )

        env = build_sanitized_environment(
            os.environ,
            workspace_dir=self.workspace_dir,
            extra_env=extra_env,
            trusted_env=trusted_env,
        )

        timed_out = False
        try:
            max_cap = max(8192, self.max_output_chars * 4)
            exit_code, stdout, stderr, timed_out = _run_bounded_subprocess(
                argv,
                cwd=str(self.workspace_dir),
                env=env,
                timeout=timeout,
                max_capture_bytes=max_cap,
                hard_kill_bytes=max(1_000_000, max_cap * 8),
                kill_callback=self._kill_process_tree,
            )
            if timed_out:
                stderr = (stderr or "") + f"\n[WATCHDOG] Process terminated after exceeding {timeout}s."
        except FileNotFoundError:
            exit_code = 127
            stdout = ""
            stderr = f"Command not found: '{argv[0]}'"
        except Exception as e:
            exit_code = -1
            stdout = ""
            stderr = f"Internal execution failure: {e}"

        duration_ms = (time.perf_counter() - start_time) * 1000.0

        # Output sanitization & truncation to safeguard context window
        stdout_clean = self._sanitize_and_truncate(stdout)
        stderr_clean = self._sanitize_and_truncate(stderr)

        return ExecutionResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout_clean,
            stderr=stderr_clean,
            timed_out=timed_out,
            duration_ms=round(duration_ms, 2),
        )

    def _sanitize_and_truncate(self, text: str) -> str:
        """Strip terminal controls and truncate excessive output."""
        return sanitize_and_truncate_output(text, self.max_output_chars)
