"""Contained workspace runner with path confinement and execution watchdogs."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional
from pydantic import BaseModel, Field
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
            parts.append(f"[TIMEOUT] Command exceeded time limit and was terminated.")
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
}

_LOADER_ENV_KEYS = {
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONHOME",
    "NODE_OPTIONS",
}


def _scrub_environment(
    env: os._Environ | Dict[str, str],
    *,
    trusted_loader_keys: frozenset[str] = frozenset(),
    reject_path: bool = False,
) -> Dict[str, str]:
    """Remove secrets and process-loader overrides from a child environment."""
    clean_env = dict(env)
    for k in list(clean_env.keys()):
        upper_k = k.upper()
        if (
            upper_k in _SENSITIVE_ENV_KEYS
            or (upper_k in _LOADER_ENV_KEYS and upper_k not in trusted_loader_keys)
            or upper_k.startswith("GIT_CONFIG_")
            or upper_k == "DATABASE_URL"
            or upper_k.endswith(("_API_KEY", "_SECRET", "_TOKEN", "_SECRET_KEY", "_PASSWORD", "_DSN", "_PRIVATE_KEY", "_KEY", "_CREDENTIALS"))
            or upper_k.startswith(("GOOGLE_", "AZURE_", "SLACK_"))
            or "_AUTH" in upper_k
            or "_PASS" in upper_k
            or (reject_path and upper_k == "PATH")
        ):
            del clean_env[k]
    return clean_env


class SandboxRunner:
    """Manages workspace file isolation and subprocess execution."""

    def __init__(
        self,
        workspace_dir: str | Path,
        default_timeout_seconds: int = 60,
        max_output_chars: int = 8000,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).resolve()
        self.default_timeout = default_timeout_seconds
        self.max_output_chars = max_output_chars

        # Ensure sandbox directory exists
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def resolve_path(self, target_path: str | Path) -> Path:
        """Resolve a path and ensure it cannot escape the workspace root.
        
        Raises PermissionError if the path attempts traversal outside workspace.
        """
        raw_path = Path(target_path)
        if raw_path.is_absolute():
            resolved = raw_path.resolve()
        else:
            resolved = (self.workspace_dir / raw_path).resolve()

        # Confinement check
        try:
            resolved.relative_to(self.workspace_dir)
        except ValueError:
            raise PermissionError(
                f"Path traversal detected: '{target_path}' resolves to '{resolved}', "
                f"which is outside sandbox workspace '{self.workspace_dir}'."
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

    def run_command(
        self,
        command: str,
        timeout_seconds: Optional[int] = None,
        extra_env: Optional[Dict[str, str]] = None,
        *,
        trusted_env: Optional[Dict[str, str]] = None,
    ) -> ExecutionResult:
        """Run a shell command with timeout protection inside the workspace.

        Use ``trusted_env`` only for harness-owned values needed by DoD checks.
        The model-facing tool interface cannot set this environment mapping.
        """
        timeout = timeout_seconds or self.default_timeout
        start_time = time.perf_counter()

        base_env = os.environ.copy()
        base_path = next((value for key, value in base_env.items() if key.upper() == "PATH"), None)
        env = dict(base_env)
        env["PAGER"] = "cat"  # Prevent git or man from opening interactive pagers
        env["PYTHONUNBUFFERED"] = "1"
        if extra_env:
            env.update(extra_env)
            if any(key.upper() == "PATH" for key in extra_env):
                for key in list(env):
                    if key.upper() == "PATH":
                        del env[key]
                if base_path is not None:
                    env["PATH"] = base_path
        trusted_loader_keys = frozenset((trusted_env or {}).keys()) & frozenset({"PYTHONPATH"})
        env = _scrub_environment(env, trusted_loader_keys=trusted_loader_keys)
        if trusted_env:
            safe_trusted_env = _scrub_environment(
                trusted_env,
                trusted_loader_keys=trusted_loader_keys,
                reject_path=True,
            )
            env.update(safe_trusted_env)

        timed_out = False
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(self.workspace_dir),
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=(os.name != "nt"),
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = -1
                self._kill_process_tree(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except Exception:
                    stdout, stderr = "", ""
                stderr = (stderr or "") + f"\n[WATCHDOG] Process terminated after exceeding {timeout}s."
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
        text = strip_ansi(text)
        if len(text) <= self.max_output_chars:
            return text

        omitted_count = len(text) - self.max_output_chars
        head_chars = self.max_output_chars // 2
        tail_chars = self.max_output_chars // 2

        return (
            text[:head_chars]
            + f"\n\n... [TRUNCATED: {omitted_count} characters omitted to protect context window] ...\n\n"
            + text[-tail_chars:]
        )
