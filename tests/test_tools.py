"""Tests for coding primitives and tool registry."""

import tempfile
import unittest
from pathlib import Path
from sandbox.runner import SandboxRunner
from tools.primitives import list_dir, patch_file, read_file, run_command, write_file
from tools.registry import ToolRegistry


class TestToolPrimitives(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = SandboxRunner(self.workspace)
        self.registry = ToolRegistry(self.runner)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_write_and_read_file(self):
        write_res = write_file(self.runner, "src/main.py", "def add(a, b):\n    return a + b\n")
        self.assertFalse(write_res.is_error)
        self.assertTrue((self.workspace / "src/main.py").exists())

        read_res = read_file(self.runner, "src/main.py")
        self.assertFalse(read_res.is_error)
        self.assertIn("def add", read_res.output)
        self.assertIn("1 | def add(a, b):", read_res.output)

    def test_read_missing_file_returns_clean_error(self):
        res = read_file(self.runner, "nonexistent.py")
        self.assertTrue(res.is_error)
        self.assertIn("does not exist", res.output)

    def test_patch_file(self):
        write_file(self.runner, "code.py", "val = 10\nprint(val)\n")
        patch_res = patch_file(self.runner, "code.py", target_snippet="val = 10", replacement_snippet="val = 20")
        self.assertFalse(patch_res.is_error)
        self.assertIn("Unified Diff", patch_res.output)

        content = (self.workspace / "code.py").read_text()
        self.assertIn("val = 20", content)

    def test_list_dir(self):
        write_file(self.runner, "a.txt", "hello")
        write_file(self.runner, "nested/b.txt", "world")

        res = list_dir(self.runner, ".", recursive=False)
        self.assertFalse(res.is_error)
        self.assertIn("a.txt", res.output)
        self.assertIn("nested", res.output)

    def test_registry_dispatch(self):
        defs = self.registry.get_definitions()
        names = [d.name for d in defs]
        self.assertIn("read_file", names)
        self.assertIn("write_file", names)
        self.assertIn("run_command", names)

        # Dispatch write_file via registry
        res = self.registry.dispatch("write_file", {"path": "test.txt", "content": "from registry"})
        self.assertFalse(res.is_error)
        self.assertTrue((self.workspace / "test.txt").exists())

        # Dispatch unrecognized tool
        unknown_res = self.registry.dispatch("nonexistent_tool", {})
        self.assertTrue(unknown_res.is_error)
        self.assertIn("not recognized", unknown_res.output)


if __name__ == "__main__":
    unittest.main()
