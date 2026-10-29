"""Pillar 4: Adversarial fault injection and context safety tests."""

import shutil
import tempfile
import unittest
from pathlib import Path
from agent_core.types import UserStory
from agile.backlog import BacklogManager
from sandbox.runner import SandboxRunner
from tools.primitives import patch_file, read_file, write_file
from tools.registry import ToolRegistry

# Guard for tests that require a live Docker daemon.
_DOCKER_AVAILABLE = shutil.which("docker") is not None
if _DOCKER_AVAILABLE:
    import subprocess as _sp
    try:
        _r = _sp.run(["docker", "info"], capture_output=True, timeout=5)
        _DOCKER_AVAILABLE = _r.returncode == 0
    except Exception:
        _DOCKER_AVAILABLE = False


class TestFaultToleranceAndSafety(unittest.TestCase):
    """Verifies that the agent sandbox safely isolates the host environment during failures."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = SandboxRunner(
            workspace_dir=self.workspace,
            default_timeout_seconds=2,
            max_output_chars=500,
        )
        self.registry = ToolRegistry(self.runner)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_path_traversal_write_blocked(self) -> None:
        """Verify that attempting to write outside workspace returns a clean error."""
        result = write_file(self.runner, path="../../escape_test.txt", content="malicious")
        self.assertTrue(result.is_error)
        self.assertIn("Path traversal detected", result.output)
        self.assertFalse((self.workspace.parent / "escape_test.txt").exists())

    def test_path_traversal_read_blocked(self) -> None:
        """Verify that attempting to read outside workspace is blocked."""
        result = read_file(self.runner, path="../../windows/system.ini")
        self.assertTrue(result.is_error)
        self.assertIn("Path traversal detected", result.output)

    def test_path_traversal_patch_blocked(self) -> None:
        """Verify that patch_file blocks path traversal."""
        result = patch_file(
            self.runner,
            path="../../outside.py",
            target_snippet="old",
            replacement_snippet="new",
        )
        self.assertTrue(result.is_error)
        self.assertIn("Path traversal detected", result.output)

    def test_runaway_process_terminated_by_watchdog(self) -> None:
        """Verify that an infinite loop process is terminated by the sandbox watchdog."""
        res = self.runner.run_command(
            'python -c "import time; time.sleep(10)"',
            timeout_seconds=1,
        )
        self.assertTrue(res.timed_out)
        self.assertFalse(res.is_success)
        self.assertIn("[WATCHDOG] Process terminated", res.stderr)

    def test_context_flooding_output_truncated(self) -> None:
        """Verify that runaway command output is truncated to protect LLM context windows."""
        # Emit 5,000 characters (max_output_chars set to 500 in setUp)
        res = self.runner.run_command('python -c "print(\'X\' * 5000)"')
        self.assertEqual(res.exit_code, 0)
        self.assertIn("[TRUNCATED:", res.stdout)
        self.assertLessEqual(len(res.stdout), 800)

    def test_registry_unrecognized_tool_dispatch_fails_gracefully(self) -> None:
        """Verify that hallucinated tool invocations return safe error results."""
        res = self.registry.dispatch("execute_arbitrary_shell_payload", {"code": "rm -rf /"})
        self.assertTrue(res.is_error)
        self.assertIn("is not recognized", res.output)

    def test_registry_invalid_arguments_fails_gracefully(self) -> None:
        """Verify that tools dispatched with invalid arguments fail without unhandled crash."""
        # write_file requires 'path' and 'content'
        res = self.registry.dispatch("write_file", {"wrong_argument": 123})
        self.assertTrue(res.is_error)
        self.assertIn("Invalid arguments", res.output)

    def test_syntax_error_caught_by_dod_gate(self) -> None:
        """Verify that uncompilable code fails the syntax validation check in verify_dod."""
        # Write file with broken syntax
        (self.workspace / "syntax_err.py").write_text("def broken_syntax(:\n pass\n", encoding="utf-8")

        backlog = BacklogManager.from_epic("Syntax Gate Test")
        story = UserStory(title="Syntax Check", human_interaction_contract="`fn()` works.")
        dod = backlog.verify_dod(story=story, runner=self.runner)

        self.assertFalse(dod.syntax_valid)
        self.assertFalse(dod.is_complete)


@unittest.skipUnless(_DOCKER_AVAILABLE, "Docker daemon not available — skipping Docker fault injection tests.")
class TestDockerFaultInjection(unittest.TestCase):
    """Adversarial fault injection tests using DockerSandboxRunner."""

    def setUp(self) -> None:
        from sandbox.docker_runner import DockerSandboxRunner
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = DockerSandboxRunner(
            workspace_dir=self.workspace,
            default_timeout_seconds=5,
            max_output_chars=500,
        )
        self.registry = ToolRegistry(self.runner)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_docker_path_traversal_blocked(self) -> None:
        """Verify DockerSandboxRunner blocks path traversal before the container starts."""
        result = write_file(self.runner, path="../../escape_via_docker.txt", content="evil")
        self.assertTrue(result.is_error)
        self.assertIn("Path traversal detected", result.output)
        escaped = self.workspace.parent / "escape_via_docker.txt"
        self.assertFalse(escaped.exists(), "Traversal file must not appear on the host.")

    def test_docker_runaway_process_terminated_by_watchdog(self) -> None:
        """Verify that an infinite sleep inside a container is killed by the watchdog."""
        result = self.runner.run_command("sleep 60", timeout_seconds=2)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.is_success)
        self.assertIn("[WATCHDOG]", result.stderr)

    def test_docker_context_flooding_output_truncated(self) -> None:
        """Verify that excessive container output is truncated with max_output_chars=500."""
        result = self.runner.run_command("python3 -c \"print('Y' * 5000)\"")
        self.assertIn("[TRUNCATED:", result.stdout)
        self.assertLessEqual(len(result.stdout), 800)

    def test_docker_registry_unrecognized_tool_fails_gracefully(self) -> None:
        """Verify that hallucinated tool names fail safely through DockerSandboxRunner."""
        result = self.registry.dispatch("rm_rf_everything", {"path": "/"})
        self.assertTrue(result.is_error)
        self.assertIn("is not recognized", result.output)


if __name__ == "__main__":
    unittest.main()
