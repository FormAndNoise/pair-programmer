"""End-to-end multi-story sprint integration test with role rotation and TUI rendering."""

import tempfile
import unittest
from pathlib import Path
from rich.console import Console
from agent_core.events import EventBus
from agent_core.types import AgentConfig, ToolCall, UserStory
from agile.backlog import BacklogManager
from agile.pair_engine import PairProgrammingEngine
from gateway.mock import ScriptedMockGateway
from sandbox.runner import SandboxRunner
from tools.registry import ToolRegistry
from tui.app import PairProgrammingTUI


class TestE2EMockSprint(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = SandboxRunner(self.workspace)
        self.registry = ToolRegistry(self.runner)
        self.mock_gateway = ScriptedMockGateway()
        self.bus = EventBus()
        self.console = Console(record=True, width=120, height=40)
        self.tui = PairProgrammingTUI(bus=self.bus, console=self.console)
        self.config = AgentConfig(
            model_a="claude-3-5",
            model_b="gemini-2-5",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_full_two_story_sprint_with_role_rotation(self):
        backlog_mgr = BacklogManager.from_epic("Build String & Math Utilities")

        # Story 1: String Formatter (Story 0 -> Model A = Test, Model B = Impl)
        s1 = backlog_mgr.add_story(
            title="Capitalize Words",
            human_interaction_contract="`capitalize_words('hello world')` returns `'Hello World'`.",
        )

        # Story 2: Math Multiplier (Story 1 -> Model B = Test, Model A = Impl - ROTATION!)
        s2 = backlog_mgr.add_story(
            title="Multiply Numbers",
            human_interaction_contract="`multiply(3, 4)` returns `12`.",
        )

        # -------------------------------------------------------------
        # Scripting Story 1 Responses
        # -------------------------------------------------------------
        # 1. Model A writes test
        test_str_code = (
            "import unittest\n"
            "from str_util import capitalize_words\n"
            "class TestStr(unittest.TestCase):\n"
            "    def test_cap(self):\n"
            "        self.assertEqual(capitalize_words('hello world'), 'Hello World')\n"
            "if __name__ == '__main__': unittest.main()\n"
        )
        self.mock_gateway.queue_text_response(
            model="claude-3-5",
            content="Writing tests for capitalize_words.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": "test_str.py", "content": test_str_code})],
        )

        # 2. Model B writes implementation
        impl_str_code = "def capitalize_words(s: str) -> str: return s.title()\n"
        self.mock_gateway.queue_text_response(
            model="gemini-2-5",
            content="Implementing capitalize_words.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": "str_util.py", "content": impl_str_code})],
        )

        # 3. Model A reviews and approves
        self.mock_gateway.queue_text_response(
            model="claude-3-5",
            content="Tests pass and UX contract is satisfied. LGTM.",
        )

        # -------------------------------------------------------------
        # Scripting Story 2 Responses (ROLES ROTATED)
        # -------------------------------------------------------------
        # 4. Model B writes test (Now Model B is Test Author!)
        test_math_code = (
            "import unittest\n"
            "from mul_util import multiply\n"
            "class TestMul(unittest.TestCase):\n"
            "    def test_mul(self):\n"
            "        self.assertEqual(multiply(3, 4), 12)\n"
            "if __name__ == '__main__': unittest.main()\n"
        )
        self.mock_gateway.queue_text_response(
            model="gemini-2-5",
            content="Model B writing failing test for multiply.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": "test_mul.py", "content": test_math_code})],
        )

        # 5. Model A writes implementation (Now Model A is Implementer!)
        impl_math_code = "def multiply(a: int, b: int) -> int: return a * b\n"
        self.mock_gateway.queue_text_response(
            model="claude-3-5",
            content="Model A implementing multiply.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": "mul_util.py", "content": impl_math_code})],
        )

        # 6. Model B reviews and approves
        self.mock_gateway.queue_text_response(
            model="gemini-2-5",
            content="Fast and cleanly typed. LGTM.",
        )

        # Run the full sprint
        engine = PairProgrammingEngine(
            config=self.config,
            backlog_mgr=backlog_mgr,
            runner=self.runner,
            registry=self.registry,
            gateway=self.mock_gateway,
            bus=self.bus,
        )

        sprint_success = engine.run_sprint()
        self.assertTrue(sprint_success)
        self.assertTrue(backlog_mgr.sprint.is_complete)
        self.assertEqual(s1.status.value, "done")
        self.assertEqual(s2.status.value, "done")

        # Verify files created in sandbox
        self.assertTrue((self.workspace / "test_str.py").exists())
        self.assertTrue((self.workspace / "str_util.py").exists())
        self.assertTrue((self.workspace / "test_mul.py").exists())
        self.assertTrue((self.workspace / "mul_util.py").exists())

        # Verify TUI captured both completions
        self.assertEqual(len(self.tui.stories), 2)
        self.assertTrue(all(s["status"] == "DONE" for s in self.tui.stories))

        # Check rendered layout snapshot
        self.tui.display_once()
        rendered_output = self.console.export_text()
        self.assertIn("Build String & Math Utilities", rendered_output)
        self.assertIn("Capitalize Words", rendered_output)
        self.assertIn("Multiply Numbers", rendered_output)


if __name__ == "__main__":
    unittest.main()
