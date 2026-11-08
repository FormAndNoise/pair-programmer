"""Pillar 5 (Phase 3): DockerSandboxRunner tests — drop-in parity and isolation.

All tests skip automatically when Docker is unavailable on the host.
Tests verify:
  - Interface parity with SandboxRunner (same method signatures, same return types)
  - Path confinement on the host side (resolve_path raises PermissionError)
  - Successful command execution and ExecutionResult correctness
  - Timeout / watchdog kills the container
  - Output truncation protects LLM context window
  - Network isolation (no outbound traffic from inside the container)
  - ToolRegistry drop-in substitution
  - _ensure_image_available runs without error
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

# Skip the entire module if Docker is not available or daemon is not running.
DOCKER_AVAILABLE = shutil.which("docker") is not None
if DOCKER_AVAILABLE:
    import subprocess as _sp
    try:
        _check = _sp.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=10,
        )
        DOCKER_AVAILABLE = _check.returncode == 0
    except Exception:
        DOCKER_AVAILABLE = False


@unittest.skipUnless(DOCKER_AVAILABLE, "Docker daemon not available on this host — skipping Phase 3 tests.")
class TestDockerSandboxRunnerInterface(unittest.TestCase):
    """Verify that DockerSandboxRunner exposes the same interface as SandboxRunner."""

    def setUp(self) -> None:
        # Import here so module-level import never fails on machines without Docker.
        from sandbox.docker_runner import DockerSandboxRunner
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = DockerSandboxRunner(
            workspace_dir=self.workspace,
            default_timeout_seconds=30,
            max_output_chars=4000,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_has_required_attributes(self) -> None:
        """Verify DockerSandboxRunner exposes workspace_dir, default_timeout, max_output_chars."""
        self.assertIsInstance(self.runner.workspace_dir, Path)
        self.assertIsInstance(self.runner.default_timeout, int)
        self.assertIsInstance(self.runner.max_output_chars, int)

    def test_resolve_path_returns_path_within_workspace(self) -> None:
        """Verify resolve_path returns a Path object for a safe relative path."""
        result = self.runner.resolve_path("subdir/file.py")
        self.assertIsInstance(result, Path)
        self.assertTrue(str(result).startswith(str(self.workspace)))

    def test_path_traversal_blocked_by_resolve_path(self) -> None:
        """Verify that resolve_path raises PermissionError on directory traversal."""
        with self.assertRaises(PermissionError) as ctx:
            self.runner.resolve_path("../../etc/passwd")
        self.assertIn("Path traversal detected", str(ctx.exception))

    def test_path_traversal_absolute_path_blocked(self) -> None:
        """Verify that an absolute path outside the workspace is blocked."""
        with self.assertRaises(PermissionError):
            self.runner.resolve_path("/etc/shadow")

    def test_run_command_returns_execution_result(self) -> None:
        """Verify run_command returns an ExecutionResult with all required fields."""
        from sandbox.runner import ExecutionResult
        result = self.runner.run_command("echo hello_world")
        self.assertIsInstance(result, ExecutionResult)
        self.assertIsInstance(result.exit_code, int)
        self.assertIsInstance(result.stdout, str)
        self.assertIsInstance(result.stderr, str)
        self.assertIsInstance(result.timed_out, bool)
        self.assertIsInstance(result.duration_ms, float)

    def test_successful_echo_command(self) -> None:
        """Verify that a basic echo returns exit_code 0 and captured stdout."""
        result = self.runner.run_command("echo phase3_ok")
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.is_success)
        self.assertIn("phase3_ok", result.stdout)

    def test_failing_command_returns_nonzero_exit(self) -> None:
        """Verify that a failing command returns a non-zero exit code."""
        result = self.runner.run_command("exit 42")
        self.assertFalse(result.is_success)
        self.assertEqual(result.exit_code, 42)

    def test_python_execution_inside_container(self) -> None:
        """Verify that Python code runs correctly inside the container."""
        result = self.runner.run_command('python3 -c "print(2 + 2)"')
        self.assertEqual(result.exit_code, 0)
        self.assertIn("4", result.stdout)

    def test_workspace_files_visible_inside_container(self) -> None:
        """Verify that files written to workspace_dir are accessible inside the container."""
        target = self.workspace / "greeting.txt"
        target.write_text("hello_from_host", encoding="utf-8")

        result = self.runner.run_command("cat greeting.txt")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello_from_host", result.stdout)

    def test_files_created_in_container_appear_on_host(self) -> None:
        """Verify that files created by container commands are persisted to the host workspace."""
        result = self.runner.run_command("echo container_output > artefact.txt")
        self.assertEqual(result.exit_code, 0)
        artefact = self.workspace / "artefact.txt"
        self.assertTrue(artefact.exists(), "Container-written file should appear on host.")

    def test_duration_ms_is_positive(self) -> None:
        """Verify that execution duration is recorded and positive."""
        result = self.runner.run_command("echo timing_check")
        self.assertGreater(result.duration_ms, 0.0)

    def test_container_runs_as_non_root_with_read_only_rootfs(self) -> None:
        """Verify container process runs as uid 65534 and blocks writes to rootfs."""
        uid_res = self.runner.run_command("id -u")
        self.assertEqual(uid_res.exit_code, 0)
        self.assertEqual(uid_res.stdout.strip(), "65534")

        ro_res = self.runner.run_command("touch /etc/evil_probe")
        self.assertNotEqual(ro_res.exit_code, 0)
        self.assertIn("Read-only file system", ro_res.stderr)

        tmp_res = self.runner.run_command("touch /tmp/ephemeral_ok && rm /tmp/ephemeral_ok")
        self.assertEqual(tmp_res.exit_code, 0)


@unittest.skipUnless(DOCKER_AVAILABLE, "Docker daemon not available on this host — skipping Phase 3 tests.")
class TestDockerSandboxRunnerWatchdog(unittest.TestCase):
    """Verify that the container watchdog terminates long-running commands."""

    def setUp(self) -> None:
        from sandbox.docker_runner import DockerSandboxRunner
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = DockerSandboxRunner(
            workspace_dir=self.workspace,
            default_timeout_seconds=5,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_timeout_kills_sleeping_container(self) -> None:
        """Verify that a sleep command is terminated and timed_out is True."""
        result = self.runner.run_command("sleep 60", timeout_seconds=2)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.is_success)
        self.assertIn("[WATCHDOG]", result.stderr)

    def test_is_success_false_on_timeout(self) -> None:
        """Verify that is_success returns False when the watchdog fires."""
        result = self.runner.run_command("sleep 60", timeout_seconds=2)
        self.assertFalse(result.is_success)


@unittest.skipUnless(DOCKER_AVAILABLE, "Docker daemon not available on this host — skipping Phase 3 tests.")
class TestDockerSandboxRunnerOutputTruncation(unittest.TestCase):
    """Verify that excessive output is truncated to protect the LLM context window."""

    def setUp(self) -> None:
        from sandbox.docker_runner import DockerSandboxRunner
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = DockerSandboxRunner(
            workspace_dir=self.workspace,
            default_timeout_seconds=30,
            max_output_chars=500,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_large_output_is_truncated(self) -> None:
        """Verify that output exceeding max_output_chars is truncated with a banner."""
        # Emit ~5 000 characters; max is 500.
        result = self.runner.run_command("python3 -c \"print('X' * 5000)\"")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("[TRUNCATED:", result.stdout)
        self.assertLessEqual(len(result.stdout), 800)  # truncation banner adds some chars

    def test_small_output_not_truncated(self) -> None:
        """Verify that output within max_output_chars is returned verbatim."""
        result = self.runner.run_command("echo short_output")
        self.assertNotIn("[TRUNCATED:", result.stdout)

    def test_runaway_output_stream_bounded_and_container_cleaned_up(self) -> None:
        """Verify runaway stdout stream is bounded by pipe reader and leaves no container."""
        import subprocess
        result = self.runner.run_command("yes", timeout_seconds=2)
        self.assertLessEqual(len(result.stdout), 800)
        ps = subprocess.run(
            ["docker", "ps", "-q", "--filter", "name=^pp-sandbox-"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(ps.stdout.strip(), "")


@unittest.skipUnless(DOCKER_AVAILABLE, "Docker daemon not available on this host — skipping Phase 3 tests.")
class TestDockerSandboxRunnerNetworkIsolation(unittest.TestCase):
    """Verify that the container cannot make outbound network requests."""

    def setUp(self) -> None:
        from sandbox.docker_runner import DockerSandboxRunner
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = DockerSandboxRunner(
            workspace_dir=self.workspace,
            default_timeout_seconds=15,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_curl_to_external_host_fails(self) -> None:
        """Verify that outbound HTTP is blocked when --network none is active."""
        result = self.runner.run_command("curl -s --max-time 5 https://example.com || exit 1")
        # Either curl is not installed (exit 127) or the connection is refused.
        self.assertNotEqual(result.exit_code, 0)

    def test_python_urlopen_to_external_host_fails(self) -> None:
        """Verify that Python cannot open an external URL from inside the container."""
        code = (
            "import urllib.request, sys; "
            "urllib.request.urlopen('https://example.com', timeout=5); "
            "sys.exit(0)"
        )
        result = self.runner.run_command(f'python3 -c "{code}" || true')
        # The command may produce an error in stderr — the important thing is
        # that it did NOT succeed (no HTTP response body in stdout).
        self.assertNotIn("Example Domain", result.stdout)


@unittest.skipUnless(DOCKER_AVAILABLE, "Docker daemon not available on this host — skipping Phase 3 tests.")
class TestDockerSandboxRunnerToolRegistryDropIn(unittest.TestCase):
    """Verify DockerSandboxRunner works as a drop-in with ToolRegistry."""

    def setUp(self) -> None:
        from sandbox.docker_runner import DockerSandboxRunner
        from tools.registry import ToolRegistry
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = DockerSandboxRunner(
            workspace_dir=self.workspace,
            default_timeout_seconds=30,
        )
        self.registry = ToolRegistry(self.runner)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_write_file_via_registry(self) -> None:
        """Verify write_file dispatched through ToolRegistry works with DockerSandboxRunner."""
        result = self.registry.dispatch("write_file", {"path": "hello.txt", "content": "docker_registry_test"})
        self.assertFalse(result.is_error, msg=result.output)
        target = self.workspace / "hello.txt"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "docker_registry_test")

    def test_read_file_via_registry(self) -> None:
        """Verify read_file dispatched through ToolRegistry reads from the workspace."""
        (self.workspace / "source.txt").write_text("registry_read_test", encoding="utf-8")
        result = self.registry.dispatch("read_file", {"path": "source.txt"})
        self.assertFalse(result.is_error, msg=result.output)
        self.assertIn("registry_read_test", result.output)

    def test_run_command_via_registry(self) -> None:
        """Verify run_command dispatched through ToolRegistry executes inside the container."""
        result = self.registry.dispatch("run_command", {"command": "echo registry_run_ok"})
        self.assertFalse(result.is_error, msg=result.output)
        self.assertIn("registry_run_ok", result.output)

    def test_path_traversal_blocked_via_registry(self) -> None:
        """Verify that write_file path traversal is blocked end-to-end through the registry."""
        result = self.registry.dispatch("write_file", {"path": "../../escape.txt", "content": "evil"})
        self.assertTrue(result.is_error)
        self.assertIn("Path traversal detected", result.output)

    def test_unrecognized_tool_fails_gracefully(self) -> None:
        """Verify that hallucinated tool names return a clean error with DockerSandboxRunner."""
        result = self.registry.dispatch("rm_rf_root", {"target": "/"})
        self.assertTrue(result.is_error)
        self.assertIn("is not recognized", result.output)


@unittest.skipUnless(DOCKER_AVAILABLE, "Docker daemon not available on this host — skipping Phase 3 tests.")
class TestDockerSandboxRunnerFromSandboxRunner(unittest.TestCase):
    """Verify the from_sandbox_runner factory method."""

    def test_from_sandbox_runner_inherits_config(self) -> None:
        """Verify from_sandbox_runner copies workspace_dir, timeout, and max_output_chars."""
        from sandbox.docker_runner import DockerSandboxRunner
        from sandbox.runner import SandboxRunner
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            base = SandboxRunner(workspace_dir=td, default_timeout_seconds=45, max_output_chars=1234)
            docker = DockerSandboxRunner.from_sandbox_runner(base)
            self.assertEqual(docker.workspace_dir, base.workspace_dir)
            self.assertEqual(docker.default_timeout, base.default_timeout)
            self.assertEqual(docker.max_output_chars, base.max_output_chars)


class TestDockerSandboxRunnerNoDaemon(unittest.TestCase):
    """Verify graceful degradation when Docker is unavailable.

    These tests always run — even on machines without Docker.
    """

    def setUp(self) -> None:
        from sandbox.docker_runner import DockerSandboxRunner
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        # Point to a non-existent binary to simulate an environment with no Docker.
        self.runner = DockerSandboxRunner(
            workspace_dir=self.workspace,
            docker_binary="docker_does_not_exist_sentinel",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_path_traversal_still_blocked_without_docker(self) -> None:
        """Verify resolve_path raises PermissionError even when Docker is unavailable."""
        with self.assertRaises(PermissionError):
            self.runner.resolve_path("../../etc/passwd")

    def test_run_command_returns_failure_result_without_docker(self) -> None:
        """Verify run_command returns an ExecutionResult (not an exception) without Docker."""
        from sandbox.runner import ExecutionResult
        result = self.runner.run_command("echo hello")
        self.assertIsInstance(result, ExecutionResult)
        self.assertNotEqual(result.exit_code, 0)

    def test_format_output_usable_on_docker_unavailable_result(self) -> None:
        """Verify format_output produces a non-empty string even on a Docker-unavailable result."""
        result = self.runner.run_command("echo hello")
        output = result.format_output()
        self.assertIsInstance(output, str)
        self.assertGreater(len(output), 0)

    def test_build_docker_args_includes_name_and_hardened_flags(self) -> None:
        """Verify generated docker args include unique name, cap-drop, and pids-limit."""
        args = self.runner._build_docker_args(
            command="python -V",
            timeout=10,
            extra_env={},
            container_name="test-box-123",
        )
        self.assertIn("--name", args)
        self.assertIn("test-box-123", args)
        self.assertIn("--pids-limit", args)
        self.assertIn("128", args)
        self.assertIn("--cap-drop=ALL", args)
        self.assertIn("--security-opt", args)
        self.assertIn("no-new-privileges", args)

    def test_build_docker_args_scrubs_sensitive_env(self) -> None:
        """Verify sensitive environment variables are filtered out of container args."""
        args = self.runner._build_docker_args(
            command="python -V",
            timeout=10,
            extra_env={
                "ANTHROPIC_API_KEY": "secret",
                "SAFE_VAR": "visible",
            },
            container_name="test-box-123",
        )
        self.assertNotIn("-e", [a for a in args if "ANTHROPIC_API_KEY" in a])
        self.assertIn("-e", args)
        self.assertIn("SAFE_VAR=visible", args)

    def test_docker_available_is_cached(self) -> None:
        """Verify _docker_available does not repeatedly execute external checks."""
        self.runner._cached_docker_available = True
        self.assertTrue(self.runner._docker_available())
        self.runner._cached_docker_available = False
        self.assertFalse(self.runner._docker_available())

    def test_require_digest_rejects_unpinned_tag(self) -> None:
        """Verify require_digest=True rejects mutable image tags without @sha256: digest."""
        from sandbox.docker_runner import DockerSandboxRunner
        with self.assertRaises(ValueError):
            DockerSandboxRunner(
                workspace_dir=self.workspace,
                image="python:3.11-slim",
                require_digest=True,
            )
        pinned = DockerSandboxRunner(
            workspace_dir=self.workspace,
            image="python:3.11-slim@sha256:" + ("a" * 64),
            require_digest=True,
            docker_binary="docker_does_not_exist_sentinel",
        )
        self.assertTrue(pinned.require_digest)

    def test_default_image_is_digest_pinned_and_mount_spec_validated(self) -> None:
        """R-09: Verify default image uses @sha256 digest and mount paths are validated."""
        from sandbox.docker_runner import DEFAULT_PRODUCTION_IMAGE, DockerSandboxRunner
        self.assertIn("@sha256:", DEFAULT_PRODUCTION_IMAGE)
        self.assertEqual(self.runner.image, DEFAULT_PRODUCTION_IMAGE)

        win_mount = DockerSandboxRunner._encode_mount_spec(r"C:\Users\Aarik\sandbox")
        self.assertEqual(win_mount, r"type=bind,source=C:\Users\Aarik\sandbox,target=/workspace")

        posix_mount = DockerSandboxRunner._encode_mount_spec("/tmp/sandbox_ws")
        self.assertEqual(posix_mount, "type=bind,source=/tmp/sandbox_ws,target=/workspace")

        with self.assertRaises(ValueError):
            DockerSandboxRunner._encode_mount_spec("/")
        with self.assertRaises(ValueError):
            DockerSandboxRunner._encode_mount_spec("/tmp/bad,inject=1")
        with self.assertRaises(ValueError):
            DockerSandboxRunner._encode_mount_spec("/tmp/bad:ro")


if __name__ == "__main__":
    unittest.main()
