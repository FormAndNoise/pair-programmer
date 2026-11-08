"""Tests for the standalone Outside-In UX Evaluator simulating end-user interactions."""

import tempfile
import unittest
from pathlib import Path
from agent_core.events import EventBus
from agent_core.types import AgentConfig, ToolCall, UserStory
from agile.backlog import BacklogManager
from agile.pair_engine import PairProgrammingEngine
from gateway.mock import ScriptedMockGateway
from sandbox.runner import SandboxRunner
from tools.registry import ToolRegistry
from .outside_in_evaluator import OutsideInUXEvaluator, UXScorecard


class TestOutsideInUXEvaluator(unittest.TestCase):
    """Verifies that the Outside-In UX evaluator accurately grades software usability."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = SandboxRunner(self.workspace)
        self.evaluator = OutsideInUXEvaluator(self.runner)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_successful_python_contract_evaluation(self) -> None:
        """Verify that a compliant implementation produces a passing UXScorecard."""
        # Write implementation to sandbox
        impl_code = (
            "from collections import Counter\n"
            "def count_words(text: str) -> dict:\n"
            "    return dict(Counter(text.lower().split()))\n"
        )
        (self.workspace / "word_counter.py").write_text(impl_code, encoding="utf-8")

        story = UserStory(
            title="Word Frequency Counter",
            human_interaction_contract="`count_words('apple banana apple')` returns `{'apple': 2, 'banana': 1}`.",
        )

        scorecard = self.evaluator.evaluate_story_contract(story)
        self.assertTrue(scorecard.passed)
        self.assertEqual(scorecard.exit_code, 0)
        self.assertTrue(scorecard.no_uncaught_tracebacks)
        self.assertIn("verified", scorecard.feedback)

    def test_failing_contract_returns_violating_scorecard(self) -> None:
        """Verify that a buggy or non-compliant implementation produces a failing scorecard."""
        # Intentionally faulty implementation: returns wrong counts
        broken_code = (
            "def count_words(text: str) -> dict:\n"
            "    return {'wrong': 99}\n"
        )
        (self.workspace / "word_counter.py").write_text(broken_code, encoding="utf-8")

        story = UserStory(
            title="Word Frequency Counter",
            human_interaction_contract="`count_words('apple banana apple')` returns `{'apple': 2, 'banana': 1}`.",
        )

        scorecard = self.evaluator.evaluate_story_contract(story)
        self.assertFalse(scorecard.passed)
        self.assertNotEqual(scorecard.exit_code, 0)
        self.assertIn("Violation", scorecard.feedback)

    def test_missing_function_returns_actionable_ux_barrier(self) -> None:
        """Verify that an undefined function surfaces a clean UX barrier message."""
        story = UserStory(
            title="Nonexistent Feature",
            human_interaction_contract="`missing_fn('test')` returns `'test'`.",
        )

        scorecard = self.evaluator.evaluate_story_contract(story)
        self.assertFalse(scorecard.passed)
        self.assertIn("UX Barrier", scorecard.feedback)

    def test_cli_contract_evaluation(self) -> None:
        """Verify that CLI invocation contracts are executed and validated."""
        cli_code = (
            "import sys\n"
            "if len(sys.argv) > 1 and sys.argv[1] == '--version':\n"
            "    print('App CLI v1.0.0')\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        (self.workspace / "app_cli.py").write_text(cli_code, encoding="utf-8")

        story = UserStory(
            title="Version Flag",
            human_interaction_contract="Running `python app_cli.py --version` prints version info.",
        )

        scorecard = self.evaluator.evaluate_story_contract(story)
        self.assertTrue(scorecard.passed)
        self.assertEqual(scorecard.exit_code, 0)
        self.assertIn("App CLI v1.0.0", scorecard.output_summary)

    def test_double_quoted_python_contract_evaluates_without_shell_breakage(self) -> None:
        """Verify contracts with double-quoted arguments and expectations survive shell quoting."""
        (self.workspace / "slug_mod.py").write_text(
            "def slugify(text: str) -> str:\n    return text.lower().replace(' ', '-')\n",
            encoding="utf-8",
        )
        story = UserStory(
            title="Slugify",
            human_interaction_contract='`slugify("Hello World")` returns `"hello-world"`.',
        )
        scorecard = self.evaluator.evaluate_story_contract(story)
        self.assertTrue(scorecard.passed, scorecard.output_summary)

    def test_non_python_backtick_token_is_never_executed_as_shell_command(self) -> None:
        """Verify arbitrary backticked text in a contract never runs as a host shell command."""
        (self.workspace / "test_noop.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n",
            encoding="utf-8",
        )
        story = UserStory(
            title="Untrusted Contract",
            human_interaction_contract="Users see `echo pwned > pwned.txt` in the help output.",
        )
        self.evaluator.evaluate_story_contract(story)
        self.assertFalse((self.workspace / "pwned.txt").exists())

    def test_python_cli_contract_with_shell_metacharacters_is_not_executed(self) -> None:
        """Verify python CLI contracts that chain shell operators are rejected, not executed."""
        (self.workspace / "app_cli.py").write_text("print('ok')\n", encoding="utf-8")
        story = UserStory(
            title="Chained CLI",
            human_interaction_contract="Running `python app_cli.py && echo pwned > pwned.txt` prints ok.",
        )
        self.evaluator.evaluate_story_contract(story)
        self.assertFalse((self.workspace / "pwned.txt").exists())

    def test_full_sprint_integration_with_ux_evaluator(self) -> None:
        """Execute a full dual-model sprint and audit both delivered stories with the evaluator."""
        registry = ToolRegistry(self.runner)
        mock_gateway = ScriptedMockGateway()
        bus = EventBus()
        config = AgentConfig(model_a="claude-3-5", model_b="gemini-2-5")

        backlog_mgr = BacklogManager.from_epic("String Utility Sprint")
        s1 = backlog_mgr.add_story(
            title="Slugify Helper",
            human_interaction_contract="`slugify('Hello World!')` returns `'hello-world'`.",
        )

        # 1. Model A writes test
        test_code = (
            "import unittest\n"
            "from slug_mod import slugify\n"
            "class TestSlug(unittest.TestCase):\n"
            "    def test_slug(self):\n"
            "        self.assertEqual(slugify('Hello World!'), 'hello-world')\n"
            "if __name__ == '__main__': unittest.main()\n"
        )
        mock_gateway.queue_text_response(
            model="claude-3-5",
            content="Writing test.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": "test_slug.py", "content": test_code})],
        )

        # 2. Model B writes implementation
        impl_code = (
            "import re\n"
            "def slugify(text: str) -> str:\n"
            "    cleaned = re.sub(r'[^a-zA-Z0-9\\s]', '', text).strip().lower()\n"
            "    return re.sub(r'\\s+', '-', cleaned)\n"
        )
        mock_gateway.queue_text_response(
            model="gemini-2-5",
            content="Implementing slugify.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": "slug_mod.py", "content": impl_code})],
        )

        # 3. Model A reviews
        mock_gateway.queue_text_response(model="claude-3-5", content="Clean code. LGTM.")

        engine = PairProgrammingEngine(
            config=config,
            backlog_mgr=backlog_mgr,
            runner=self.runner,
            registry=registry,
            gateway=mock_gateway,
            bus=bus,
        )

        sprint_ok = engine.run_sprint()
        self.assertTrue(sprint_ok)

        # Standalone outside-in UX evaluation pass
        scorecard = self.evaluator.evaluate_story_contract(s1)
        self.assertTrue(scorecard.passed)
        self.assertEqual(scorecard.exit_code, 0)
        self.assertIn("verified", scorecard.feedback)


if __name__ == "__main__":
    unittest.main()
