"""Contained workspace runner with path confinement and execution watchdogs."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional
from pydantic import BaseModel, Field


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

    def run_command(
        self,
        command: str,
        timeout_seconds: Optional[int] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> ExecutionResult:
        """Run a shell command confined to the workspace with timeout protection."""
        timeout = timeout_seconds or self.default_timeout
        start_time = time.perf_counter()

        env = os.environ.copy()
        env["PAGER"] = "cat"  # Prevent git or man from opening interactive pagers
        env["PYTHONUNBUFFERED"] = "1"
        if extra_env:
            env.update(extra_env)

        timed_out = False
        try:
            proc = subprocess.run(
                command,
                cwd=str(self.workspace_dir),
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode

        except subprocess.TimeoutExpired as e:
            timed_out = True
            exit_code = -1
            stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
            stderr += f"\n[WATCHDOG] Process terminated after exceeding {timeout}s."

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
        """Truncate excessive output strings with an informative banner."""
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
