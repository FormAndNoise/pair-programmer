"""Tests for the PairProgrammingTUI component."""

import unittest
from rich.console import Console
from rich.layout import Layout
from agent_core.events import AgentEvent, EventBus, EventType
from tui.app import PairProgrammingTUI


class TestTUI(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        # Null / recording console for test execution
        self.console = Console(record=True, width=120, height=40)
        self.tui = PairProgrammingTUI(bus=self.bus, console=self.console)

    def test_tui_event_ingestion(self):
        # 1. Publish SPRINT_START
        self.bus.publish(
            AgentEvent(
                event_type=EventType.SPRINT_START,
                payload={"epic": "Build REST Authentication Service"},
            )
        )
        self.assertEqual(self.tui.epic_goal, "Build REST Authentication Service")

        # 2. Publish STORY_START
        self.bus.publish(
            AgentEvent(
                event_type=EventType.STORY_START,
                payload={"story_id": "US-01", "title": "Token Expiry Logic"},
            )
        )
        self.assertEqual(self.tui.active_story_title, "Token Expiry Logic")
        self.assertEqual(len(self.tui.stories), 1)

        # 3. Publish MODEL_MESSAGE
        self.bus.publish(
            AgentEvent(
                event_type=EventType.MODEL_MESSAGE,
                source_model="claude-3-5",
                payload={"content": "I designed the CLI syntax.", "role": "driver"},
            )
        )
        self.assertEqual(len(self.tui.dialogue_history), 1)
        self.assertEqual(self.tui.dialogue_history[0]["model"], "claude-3-5")

        # 4. Publish TOOL_RESULT
        self.bus.publish(
            AgentEvent(
                event_type=EventType.TOOL_RESULT,
                payload={"tool_name": "run_command", "output": "pytest tests/ - 4 passed in 0.2s"},
            )
        )
        self.assertEqual(len(self.tui.vm_logs), 1)
        self.assertIn("pytest", self.tui.vm_logs[0])

        # 5. Publish DOD_EVALUATION
        self.bus.publish(
            AgentEvent(
                event_type=EventType.DOD_EVALUATION,
                payload={
                    "syntax_valid": True,
                    "tests_passed": True,
                    "cross_review_approved": True,
                    "user_experience_verified": True,
                },
            )
        )
        self.assertTrue(self.tui.dod_state["tests_passed"])
        self.assertTrue(self.tui.dod_state["cross_review_approved"])

    def test_render_layout_returns_valid_renderable(self):
        # Ingest sample data
        self.bus.publish(AgentEvent(event_type=EventType.SPRINT_START, payload={"epic": "Test Epic"}))
        self.bus.publish(AgentEvent(event_type=EventType.STORY_START, payload={"story_id": "US-01", "title": "Story 1"}))

        layout = self.tui.render_layout()
        self.assertIsInstance(layout, Layout)

        # Ensure no exceptions during render pass
        self.tui.display_once()
        rendered_text = self.console.export_text()
        self.assertIn("AGILE PAIR-PROGRAMMING STUDIO", rendered_text)
        self.assertIn("Test Epic", rendered_text)
        self.assertIn("Story 1", rendered_text)


if __name__ == "__main__":
    unittest.main()
