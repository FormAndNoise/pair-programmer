"""Tests for sandbox runner and path confinement."""

import tempfile
import unittest
from pathlib import Path
from sandbox.runner import SandboxRunner


class TestSandboxRunner(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = SandboxRunner(
            workspace_dir=self.workspace,
            default_timeout_seconds=2,
            max_output_chars=500,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_path_confinement_valid(self):
        p = self.runner.resolve_path("subfolder/file.txt")
        self.assertTrue(p.is_relative_to(self.workspace))

    def test_path_confinement_traversal_blocked(self):
        with self.assertRaises(PermissionError):
            self.runner.resolve_path("../../windows/system32")

    def test_run_command_success(self):
        res = self.runner.run_command("python -c \"print('Hello from Sandbox')\"")
        self.assertEqual(res.exit_code, 0)
        self.assertIn("Hello from Sandbox", res.stdout)
        self.assertFalse(res.timed_out)

    def test_run_command_failure_exit_code(self):
        res = self.runner.run_command("python -c \"import sys; sys.exit(42)\"")
        self.assertEqual(res.exit_code, 42)
        self.assertFalse(res.timed_out)

    def test_run_command_timeout(self):
        # 5 second sleep should exceed the 1 second timeout
        res = self.runner.run_command("python -c \"import time; time.sleep(5)\"", timeout_seconds=1)
        self.assertTrue(res.timed_out)
        self.assertIn("WATCHDOG", res.stderr)

    def test_output_truncation(self):
        # Generate 2000 chars of output (runner limit is 500)
        res = self.runner.run_command("python -c \"print('A' * 2000)\"")
        self.assertIn("TRUNCATED", res.stdout)
        self.assertLessEqual(len(res.stdout), 800)

    def test_sensitive_env_vars_are_scrubbed_from_command_execution(self):
        import os
        old_env = {}
        sensitive_vars = {
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "GEMINI_API_KEY": "gemini-secret",
            "OPENAI_API_KEY": "openai-secret",
            "OPENROUTER_API_KEY": "openrouter-secret",
            "SERVICE_TOKEN": "token-123",
            "CUSTOM_SECRET_KEY": "key-456",
        }
        for k, v in sensitive_vars.items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v

        try:
            res = self.runner.run_command(
                "python -c \""
                "import os; "
                "print('ANTHROPIC:', os.environ.get('ANTHROPIC_API_KEY', 'CLEARED')); "
                "print('GEMINI:', os.environ.get('GEMINI_API_KEY', 'CLEARED')); "
                "print('TOKEN:', os.environ.get('SERVICE_TOKEN', 'CLEARED')); "
                "\""
            )
            self.assertEqual(res.exit_code, 0)
            self.assertIn("ANTHROPIC: CLEARED", res.stdout)
            self.assertIn("GEMINI: CLEARED", res.stdout)
            self.assertIn("TOKEN: CLEARED", res.stdout)
        finally:
            for k, prev in old_env.items():
                if prev is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = prev


if __name__ == "__main__":
    unittest.main()
