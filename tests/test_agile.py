"""Tests for Agile Backlog and Pair Programming Engine."""

import tempfile
import unittest
from pathlib import Path
from agent_core.events import EventBus, EventType
from agent_core.types import AgentConfig, Message, Role, ToolCall, UserStory
from agile.backlog import BacklogManager
from agile.pair_engine import PairProgrammingEngine
from gateway.mock import ScriptedMockGateway
from sandbox.runner import SandboxRunner
from tools.registry import ToolRegistry


class TestAgilePairEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = SandboxRunner(self.workspace)
        self.registry = ToolRegistry(self.runner)
        self.mock_gateway = ScriptedMockGateway()
        self.bus = EventBus()
        self.config = AgentConfig(
            model_a="claude-3-5",
            model_b="gemini-2-5",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_backlog_creation_and_story_addition(self):
        backlog_mgr = BacklogManager.from_epic("Build Calculator CLI")
        story = backlog_mgr.add_story(
            title="Addition Support",
            human_interaction_contract="Running `calc add 2 3` prints `5` clearly.",
        )
        self.assertEqual(len(backlog_mgr.sprint.stories), 1)
        self.assertEqual(story.title, "Addition Support")

    def test_end_to_end_pair_programming_tdd_cycle(self):
        backlog_mgr = BacklogManager.from_epic("Create Math Module")
        story = backlog_mgr.add_story(
            title="Addition Function",
            human_interaction_contract="`add(2, 3)` returns `5`.",
            acceptance_criteria=["add(2, 3) == 5", "add(-1, 1) == 0"],
        )

        test_code = (
            "import unittest\n"
            "from math_mod import add\n"
            "class TestMath(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n"
            "if __name__ == '__main__': unittest.main()\n"
        )

        impl_code = (
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )

        # 1. Queue Model A response (Test Author: writes test file)
        self.mock_gateway.queue_text_response(
            model="claude-3-5",
            content="I am writing the failing unit test.",
            tool_calls=[
                ToolCall(
                    name="write_file",
                    arguments={"path": "test_math.py", "content": test_code},
                )
            ],
        )

        # 2. Queue Model B response (Implementer: writes implementation file)
        self.mock_gateway.queue_text_response(
            model="gemini-2-5",
            content="I am implementing the add function to turn tests green.",
            tool_calls=[
                ToolCall(
                    name="write_file",
                    arguments={"path": "math_mod.py", "content": impl_code},
                )
            ],
        )

        # 3. Queue Model A response (Reviewer: audits and approves)
        self.mock_gateway.queue_text_response(
            model="claude-3-5",
            content="The code is concise, correct, and readable. LGTM.",
        )

        events_captured = []
        self.bus.subscribe_all(lambda e: events_captured.append(e.event_type))

        engine = PairProgrammingEngine(
            config=self.config,
            backlog_mgr=backlog_mgr,
            runner=self.runner,
            registry=self.registry,
            gateway=self.mock_gateway,
            bus=self.bus,
        )

        # Execute the story cycle
        success = engine.execute_story_cycle(story)
        self.assertTrue(success)
        self.assertTrue(story.dod.is_complete)
        self.assertTrue((self.workspace / "test_math.py").exists())
        self.assertTrue((self.workspace / "math_mod.py").exists())

        # Verify event sequence
        self.assertIn(EventType.STORY_START, events_captured)
        self.assertIn(EventType.STORY_STATUS_CHANGE, events_captured)
        self.assertIn(EventType.TOOL_START, events_captured)
        self.assertIn(EventType.TOOL_RESULT, events_captured)
        self.assertIn(EventType.CRITIQUE_RECEIVED, events_captured)
        self.assertIn(EventType.DOD_EVALUATION, events_captured)
        self.assertIn(EventType.STORY_DONE, events_captured)

    def test_dod_ux_gate_runs_outside_in_evaluator(self):
        """The fourth DoD gate runs the Outside-In UX evaluator and rejects contract mismatches."""
        backlog_mgr = BacklogManager.from_epic("Math Module")
        story = backlog_mgr.add_story(
            title="Addition Function",
            human_interaction_contract="`add(2, 3)` returns `99`.",
        )
        # Valid impl (returns 5) + passing test, so only the UX gate can fail.
        (self.workspace / "math_mod.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8"
        )
        (self.workspace / "test_math.py").write_text(
            "import unittest\n"
            "from math_mod import add\n"
            "class TestMath(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n"
            "if __name__ == '__main__': unittest.main()\n",
            encoding="utf-8",
        )

        dod = backlog_mgr.verify_dod(story, self.runner, cross_review_approved=True, require_ux_audit=True)
        self.assertFalse(dod.user_experience_verified)
        self.assertFalse(dod.is_complete)

        # Bypassing the UX gate completes the story.
        story2 = backlog_mgr.add_story(
            title="Addition Bypass",
            human_interaction_contract="`add(2, 3)` returns `99`.",
        )
        dod2 = backlog_mgr.verify_dod(story2, self.runner, cross_review_approved=True, require_ux_audit=False)
        self.assertTrue(dod2.user_experience_verified)
        self.assertTrue(dod2.is_complete)


if __name__ == "__main__":
    unittest.main()
