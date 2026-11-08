"""Docker / MicroVM container driver — drop-in replacement for SandboxRunner.

Executes agent-generated commands inside an ephemeral Docker container.
The host workspace is bind-mounted read-write. The network is disabled so
container code cannot make outbound calls.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from sandbox.runner import (
    ExecutionResult,
    SandboxRunner,
    _run_bounded_subprocess,
    _scrub_environment,
    sanitize_and_truncate_output,
)


_DIGEST_PATTERN = re.compile(r"@sha256:[0-9a-fA-F]{64}$")


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
        require_digest: Optional[bool] = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).resolve()
        self.default_timeout = max(1, min(600, int(default_timeout_seconds)))
        self.max_output_chars = max(100, min(100_000, int(max_output_chars)))
        self.image = image
        self.docker_binary = docker_binary
        if require_digest is None:
            env_req = os.environ.get("SANDBOX_REQUIRE_DIGEST", "0").strip().lower()
            self.require_digest = env_req in {"1", "true", "yes"}
        else:
            self.require_digest = bool(require_digest)

        if self.require_digest and not _DIGEST_PATTERN.search(self.image):
            raise ValueError(
                f"SANDBOX_REQUIRE_DIGEST is enabled, but image '{self.image}' is not pinned "
                "to an immutable '@sha256:<64-hex>' digest."
            )

        self.resolved_image_digest: Optional[str] = None
        self._cached_docker_available: Optional[bool] = None
        self._image_verified: bool = False
        self._orphans_reaped: bool = False

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
        candidate = raw_path if raw_path.is_absolute() else (self.workspace_dir / raw_path)
        resolved = candidate.resolve()

        try:
            resolved.relative_to(self.workspace_dir)
        except ValueError:
            raise PermissionError(
                f"Path traversal detected: '{target_path}' resolves to '{resolved}', "
                f"which is outside sandbox workspace '{self.workspace_dir}'."
            )

        if candidate.is_symlink():
            link_target = candidate.resolve(strict=False)
            try:
                link_target.relative_to(self.workspace_dir)
            except ValueError:
                raise PermissionError(
                    f"Symlink escape detected: '{target_path}' links outside sandbox workspace."
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
        if timeout_seconds is not None and int(timeout_seconds) <= 0:
            return ExecutionResult(
                command=command,
                exit_code=-1,
                stdout="",
                stderr="Invalid timeout_seconds: must be a positive integer.",
                timed_out=False,
                duration_ms=0.0,
            )

        if not self._docker_available():
            result = DOCKER_UNAVAILABLE_RESULT.model_copy(update={"command": command})
            return result

        if not self._orphans_reaped:
            self._reap_orphaned_containers()
            self._orphans_reaped = True

        if not self._image_verified:
            self._ensure_image_available()
            self._image_verified = True

        timeout = max(1, min(600, int(timeout_seconds))) if timeout_seconds is not None else self.default_timeout
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
        max_stream_bytes = max(self.max_output_chars * 4, 65_536)

        try:
            def _kill_docker_proc(proc: subprocess.Popen) -> None:
                self._kill_escaped_container(container_name)
                try:
                    proc.kill()
                except Exception:
                    pass

            exit_code, stdout, stderr, timed_out = _run_bounded_subprocess(
                docker_args,
                cwd=str(self.workspace_dir),
                env=os.environ.copy(),
                timeout=timeout + 5,
                max_capture_bytes=max_stream_bytes,
                hard_kill_bytes=max(max_stream_bytes * 4, 2_000_000),
                kill_callback=_kill_docker_proc,
            )
            if timed_out:
                stderr += f"\n[WATCHDOG] Container terminated after exceeding {timeout}s."
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
            self._kill_escaped_container(container_name)

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
                [self.docker_binary, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                timeout=10,
            )
            self._cached_docker_available = (result.returncode == 0)
        except Exception:
            self._cached_docker_available = False
        return self._cached_docker_available

    def _reap_orphaned_containers(self) -> None:
        """Remove exited or dead pp-sandbox-* containers from previous runs."""
        try:
            ps = subprocess.run(
                [
                    self.docker_binary,
                    "ps",
                    "-aq",
                    "--filter",
                    "name=^pp-sandbox-",
                    "--filter",
                    "status=exited",
                    "--filter",
                    "status=dead",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if ps.returncode == 0 and ps.stdout:
                container_ids = [cid.strip() for cid in ps.stdout.splitlines() if cid.strip()]
                if container_ids:
                    subprocess.run(
                        [self.docker_binary, "rm", "-f", *container_ids],
                        capture_output=True,
                        timeout=10,
                    )
        except Exception:
            pass

    def _ensure_image_available(self) -> None:
        """Pull the configured image if it is not already present locally.

        This is a best-effort operation. Failures are intentionally swallowed
        so that callers in air-gapped environments using pre-loaded images
        continue to work.
        """
        try:
            check = subprocess.run(
                [self.docker_binary, "image", "inspect", "--format", "{{index .RepoDigests 0}}", self.image],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if check.returncode != 0:
                subprocess.run(
                    [self.docker_binary, "pull", self.image],
                    capture_output=True,
                    timeout=120,
                )
                check = subprocess.run(
                    [self.docker_binary, "image", "inspect", "--format", "{{index .RepoDigests 0}}", self.image],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            if check.returncode == 0 and check.stdout:
                self.resolved_image_digest = check.stdout.strip()
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
            "--rm",                                    # Auto-remove container on exit
            "--init",                                  # Reap zombies and forward signals
            "--network", "none",                       # Air-gapped: no outbound traffic
            "--read-only",                             # Read-only root filesystem
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",  # Ephemeral writable /tmp
            "--user", "65534:65534",                   # Unprivileged nobody:nogroup user
            "--memory", "512m",                        # Cap resident memory
            "--memory-swap", "512m",                   # Disable swap beyond memory cap
            "--cpus", "1.0",                           # Limit to 1 logical CPU
            "--pids-limit", "128",                     # Limit child processes against fork bombs
            "--ulimit", "nofile=1024:1024",            # Cap open file descriptors
            "--cap-drop=ALL",                          # Drop all capabilities
            "--security-opt", "no-new-privileges",     # Block privilege escalation
            "--stop-timeout", str(timeout),            # Docker stop grace period
            "-v", f"{host_path}:/workspace",           # Bind-mount the workspace
            "-w", "/workspace",                        # Set working directory
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

        # Always set clean non-root execution variables.
        args += [
            "-e", "HOME=/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "PYTHONUNBUFFERED=1",
            "-e", "PAGER=cat",
        ]

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
        """Truncate excessive output strings with an informative banner."""
        return sanitize_and_truncate_output(text, self.max_output_chars)

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
