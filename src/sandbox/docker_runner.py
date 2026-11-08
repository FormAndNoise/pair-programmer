"""Docker / MicroVM container driver — drop-in replacement for SandboxRunner.

Executes agent-generated commands inside an ephemeral Docker container.
The host workspace is bind-mounted read-write. The network is disabled so
container code cannot make outbound calls.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from agent_core.sanitize import strip_ansi
from sandbox.runner import ExecutionResult, SandboxRunner, _scrub_environment


# Sentinel returned when Docker is not reachable on this host.
DOCKER_UNAVAILABLE_RESULT = ExecutionResult(
    command="",
    exit_code=-1,
    stdout="",
    stderr="[DockerSandboxRunner] Docker daemon is not available on this host.",
    timed_out=False,
    duration_ms=0.0,
)


class DockerSandboxRunner:
    """Run commands inside an ephemeral Docker container with host-side path confinement.

    The class exposes the same public interface as SandboxRunner so it can be
    substituted at the composition root without any change to callers.

    Parameters
    ----------
    workspace_dir:
        Host directory to mount at ``/workspace`` inside the container.
    default_timeout_seconds:
        Per-command timeout. Docker ``--stop-timeout`` is set to this value.
    max_output_chars:
        Maximum combined stdout/stderr characters returned to the caller.
    image:
        Docker image to run. Must contain a POSIX ``sh`` and ``python3``.
    docker_binary:
        Path to the ``docker`` executable. Defaults to PATH resolution.
    """

    def __init__(
        self,
        workspace_dir: str | Path,
        default_timeout_seconds: int = 60,
        max_output_chars: int = 8000,
        image: str = "python:3.11-slim",
        docker_binary: str = "docker",
    ) -> None:
        self.workspace_dir = Path(workspace_dir).resolve()
        self.default_timeout = default_timeout_seconds
        self.max_output_chars = max_output_chars
        self.image = image
        self.docker_binary = docker_binary
        self._cached_docker_available: Optional[bool] = None

        # Ensure workspace exists on the host before any mount attempt.
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public interface — mirrors SandboxRunner exactly
    # ------------------------------------------------------------------

    def resolve_path(self, target_path: str | Path) -> Path:
        """Resolve a path and block traversal outside the workspace root.

        Raises PermissionError if the path escapes the workspace boundary.
        Confinement is enforced on the host before the container runs.
        """
        raw_path = Path(target_path)
        if raw_path.is_absolute():
            resolved = raw_path.resolve()
        else:
            resolved = (self.workspace_dir / raw_path).resolve()

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
        *,
        trusted_env: Optional[Dict[str, str]] = None,
    ) -> ExecutionResult:
        """Run a shell command inside a fresh, disposable Docker container.

        The container is air-gapped (``--network none``), memory-capped at
        512 MiB, and limited to 1 CPU. It is automatically removed after the
        command exits or times out.

        Use ``trusted_env`` only for harness-owned values needed by DoD checks.
        The model-facing tool interface cannot set this environment mapping.

        Returns ExecutionResult with ``timed_out=True`` when the watchdog fires.
        Returns a failure result when Docker is not available.
        """
        if not self._docker_available():
            result = DOCKER_UNAVAILABLE_RESULT.model_copy(update={"command": command})
            return result

        timeout = timeout_seconds or self.default_timeout
        container_name = f"pp-sandbox-{uuid.uuid4().hex[:12]}"
        combined_env = dict(extra_env or {})
        combined_env.update(trusted_env or {})
        trusted_loader_keys = frozenset((trusted_env or {}).keys()) & frozenset({"PYTHONPATH"})
        safe_env = _scrub_environment(
            combined_env,
            trusted_loader_keys=trusted_loader_keys,
            reject_path=True,
        )
        docker_args = self._build_docker_args(
            command,
            timeout,
            safe_env,
            container_name=container_name,
            trusted_loader_keys=trusted_loader_keys,
        )

        timed_out = False
        start_time = time.perf_counter()

        try:
            proc = subprocess.run(
                docker_args,
                capture_output=True,
                text=True,
                timeout=timeout + 5,  # outer Python timeout is slightly longer
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode

        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            stderr += f"\n[WATCHDOG] Container terminated after exceeding {timeout}s."
            # Targeted kill for the specific container name
            self._kill_escaped_container(container_name)

        except FileNotFoundError:
            # docker binary not found at all
            exit_code = -1
            stdout = ""
            stderr = "[DockerSandboxRunner] 'docker' executable not found on PATH."

        except Exception as exc:
            exit_code = -1
            stdout = ""
            stderr = f"Internal Docker execution failure: {exc}"

        duration_ms = (time.perf_counter() - start_time) * 1000.0

        return ExecutionResult(
            command=command,
            exit_code=exit_code,
            stdout=self._sanitize_and_truncate(stdout),
            stderr=self._sanitize_and_truncate(stderr),
            timed_out=timed_out,
            duration_ms=round(duration_ms, 2),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _docker_available(self) -> bool:
        """Return True when the Docker binary exists and the daemon responds."""
        if self._cached_docker_available is not None:
            return self._cached_docker_available

        if shutil.which(self.docker_binary) is None:
            self._cached_docker_available = False
            return False
        try:
            result = subprocess.run(
                [self.docker_binary, "info"],
                capture_output=True,
                timeout=5,
            )
            self._cached_docker_available = (result.returncode == 0)
        except Exception:
            self._cached_docker_available = False
        return self._cached_docker_available

    def _ensure_image_available(self) -> None:
        """Pull the configured image if it is not already present locally.

        This is a best-effort operation. Failures are intentionally swallowed
        so that callers in air-gapped environments using pre-loaded images
        continue to work.
        """
        try:
            check = subprocess.run(
                [self.docker_binary, "image", "inspect", self.image],
                capture_output=True,
                timeout=10,
            )
            if check.returncode != 0:
                subprocess.run(
                    [self.docker_binary, "pull", self.image],
                    capture_output=True,
                    timeout=120,
                )
        except Exception:
            pass  # Proceed; let run_command surface Docker errors naturally.

    def _build_docker_args(
        self,
        command: str,
        timeout: int,
        extra_env: Dict[str, str],
        container_name: Optional[str] = None,
        trusted_loader_keys: frozenset[str] = frozenset(),
    ) -> List[str]:
        """Build the ``docker run`` argument list for the given command."""
        # Sanitize workspace path for Docker volume syntax (no trailing slash)
        host_path = str(self.workspace_dir).rstrip("/\\")

        args: List[str] = [
            self.docker_binary, "run",
            "--rm",                            # Auto-remove container on exit
            "--network", "none",               # Air-gapped: no outbound traffic
            "--memory", "512m",                # Cap resident memory
            "--cpus", "1.0",                   # Limit to 1 logical CPU
            "--pids-limit", "128",             # Limit child processes against fork bombs
            "--cap-drop=ALL",                  # Drop all capabilities
            "--security-opt", "no-new-privileges",  # Block privilege escalation
            "--stop-timeout", str(timeout),    # Docker stop grace period
            "-v", f"{host_path}:/workspace",   # Bind-mount the workspace
            "-w", "/workspace",                # Set working directory
        ]

        if container_name:
            args += ["--name", container_name]

        # Forward scrubbed environment variables into the container.
        scrubbed_env = _scrub_environment(
            extra_env,
            trusted_loader_keys=trusted_loader_keys,
            reject_path=True,
        )
        for key, value in scrubbed_env.items():
            args += ["-e", f"{key}={value}"]

        # Always set PYTHONUNBUFFERED and PAGER for clean execution.
        args += ["-e", "PYTHONUNBUFFERED=1", "-e", "PAGER=cat"]

        args += [self.image, "sh", "-c", command]
        return args

    def _kill_escaped_container(self, container_identifier: str) -> None:
        """Attempt to kill the specified container after a timeout.

        Targeted explicitly to container_identifier to avoid killing unrelated containers.
        """
        try:
            subprocess.run(
                [self.docker_binary, "kill", container_identifier],
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                [self.docker_binary, "rm", "-f", container_identifier],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass

    def _sanitize_and_truncate(self, text: str) -> str:
        """Truncate excessive output strings with an informative banner.

        Identical logic to SandboxRunner._sanitize_and_truncate.
        """
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

    # ------------------------------------------------------------------
    # Compatibility shim: delegate to the host-side SandboxRunner
    # ------------------------------------------------------------------

    @classmethod
    def from_sandbox_runner(cls, runner: SandboxRunner, **kwargs) -> "DockerSandboxRunner":
        """Create a DockerSandboxRunner targeting the same workspace as a SandboxRunner.

        Useful when switching sandbox modes at the composition root.
        """
        return cls(
            workspace_dir=runner.workspace_dir,
            default_timeout_seconds=runner.default_timeout,
            max_output_chars=runner.max_output_chars,
            **kwargs,
        )
