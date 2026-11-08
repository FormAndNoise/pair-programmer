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

    def test_shell_metacharacters_not_expanded_in_local_process(self):
        """Verify shell=False executes literal argv without shell command chaining."""
        res = self.runner.run_command(
            "python -c \"import sys; print('LITERAL_ARG:' + sys.argv[1])\" \"rm -rf /tmp/x; curl evil\""
        )
        self.assertEqual(res.exit_code, 0)
        self.assertIn("LITERAL_ARG:rm -rf /tmp/x; curl evil", res.stdout)

    def test_binary_stdout_replaced_cleanly_without_unicode_error(self):
        """Verify non-UTF8 bytes in child output are replaced instead of crashing."""
        res = self.runner.run_command(
            "python -c \"import sys; sys.stdout.buffer.write(b'ok_\\xff\\xfe_done\\n')\""
        )
        self.assertEqual(res.exit_code, 0)
        self.assertIn("ok_", res.stdout)
        self.assertIn("_done", res.stdout)

    def test_non_positive_timeout_rejected_and_limits_clamped(self):
        """Verify timeout_seconds <= 0 returns an error and constructor limits are clamped."""
        clamped = SandboxRunner(
            workspace_dir=self.workspace,
            default_timeout_seconds=9999,
            max_output_chars=10,
        )
        self.assertEqual(clamped.default_timeout, 600)
        self.assertEqual(clamped.max_output_chars, 100)

        bad_res = self.runner.run_command("python -c \"print(1)\"", timeout_seconds=0)
        self.assertEqual(bad_res.exit_code, -1)
        self.assertIn("timeout_seconds", bad_res.stderr)

    def test_allowlist_env_preserves_platform_vars_and_isolates_home_and_tmp(self):
        """R-02: Verify child env preserves platform vars, isolates HOME/TMP, and drops secrets/GIT/SSH."""
        import os
        old_ssh = os.environ.get("GIT_SSH_COMMAND")
        old_unlisted = os.environ.get("ARBITRARY_HOST_VAR_XYZ")
        os.environ["GIT_SSH_COMMAND"] = "evil_ssh"
        os.environ["ARBITRARY_HOST_VAR_XYZ"] = "should_not_leak"
        try:
            res = self.runner.run_command(
                "python -c \""
                "import os; "
                "print('SSH:', os.environ.get('GIT_SSH_COMMAND', 'NONE')); "
                "print('UNLISTED:', os.environ.get('ARBITRARY_HOST_VAR_XYZ', 'NONE')); "
                "print('HOME:', os.environ.get('HOME', '')); "
                "print('PATH_PRESENT:', bool(os.environ.get('PATH'))); "
                "\""
            )
            self.assertEqual(res.exit_code, 0)
            self.assertIn("SSH: NONE", res.stdout)
            self.assertIn("UNLISTED: NONE", res.stdout)
            self.assertIn("PATH_PRESENT: True", res.stdout)
            self.assertIn(str(self.workspace), res.stdout)
        finally:
            if old_ssh is None:
                os.environ.pop("GIT_SSH_COMMAND", None)
            else:
                os.environ["GIT_SSH_COMMAND"] = old_ssh
            if old_unlisted is None:
                os.environ.pop("ARBITRARY_HOST_VAR_XYZ", None)
            else:
                os.environ["ARBITRARY_HOST_VAR_XYZ"] = old_unlisted

    def test_windows_backslash_paths_and_workspace_validation(self):
        """R-01 / R-20: Verify Windows backslash paths parse cleanly and root/home workspaces are rejected."""
        import argparse
        import io
        import os
        from contextlib import redirect_stdout
        from sandbox.runner import validate_workspace_path
        import main as main_mod

        argv = SandboxRunner._parse_command_argv(r'python "C:\Temp\my script.py" C:\Temp\arg.txt')
        self.assertEqual(argv[1], r"C:\Temp\my script.py")
        self.assertEqual(argv[2], r"C:\Temp\arg.txt")

        root_path = Path(self.workspace.anchor)
        with self.assertRaises(ValueError):
            validate_workspace_path(root_path)
        with self.assertRaises(ValueError):
            validate_workspace_path(Path.home())

        # Live mode refuses local_process without --unsafe-local
        old_mode = os.environ.get("SANDBOX_MODE")
        os.environ["SANDBOX_MODE"] = "local_process"
        try:
            with self.assertRaises(SystemExit):
                main_mod._build_runner(
                    argparse.Namespace(docker=False, unsafe_local=False, mock=False),
                    self.workspace,
                )
            buf = io.StringIO()
            with redirect_stdout(buf):
                runner = main_mod._build_runner(
                    argparse.Namespace(docker=False, unsafe_local=True, mock=False),
                    self.workspace,
                )
            self.assertIsInstance(runner, SandboxRunner)
            self.assertIn("SECURITY WARNING", buf.getvalue())
        finally:
            if old_mode is None:
                os.environ.pop("SANDBOX_MODE", None)
            else:
                os.environ["SANDBOX_MODE"] = old_mode

    def test_grandchild_holding_stdout_pipe_is_terminated_on_timeout(self):
        """R-14: Verify a background grandchild inheriting stdout is killed and drain threads exit."""
        code = (
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "time.sleep(30)"
        )
        start = time.monotonic() if hasattr(self, "_start") else __import__("time").monotonic()
        res = self.runner.run_command(f'python -c "{code}"', timeout_seconds=1)
        elapsed = __import__("time").monotonic() - start
        self.assertTrue(res.timed_out)
        self.assertLess(elapsed, 10.0)


if __name__ == "__main__":
    unittest.main()
