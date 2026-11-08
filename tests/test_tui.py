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

    def test_dod_badges_do_not_falsely_show_pass_before_verification(self):
        """Verify that DoD gates display WAIT rather than fake PASS during RED and GREEN phases."""
        self.bus.publish(AgentEvent(event_type=EventType.SPRINT_START, payload={"epic": "Test Epic"}))
        self.bus.publish(AgentEvent(event_type=EventType.STORY_START, payload={"story_id": "US-01", "title": "Story 1"}))
        self.bus.publish(AgentEvent(event_type=EventType.STORY_STATUS_CHANGE, payload={"status": "GREEN_IMPL"}))

        self.tui.display_once()
        rendered_text = self.console.export_text()
        # With dod_state all False, no PASS badge should appear in DoD table
        self.assertNotIn("PASS", rendered_text)

    def test_steering_buffer_and_paused_state_rendered_in_footer(self):
        """Verify footer displays steering input prompt when steering or paused."""
        self.tui.is_paused = True
        self.tui.display_once()
        rendered_paused = self.console.export_text()
        self.assertIn("PAUSED:", rendered_paused)

        self.tui.steering_buffer = "Make timeout 5s"
        self.tui.display_once()
        rendered_steer = self.console.export_text()
        self.assertIn("STEER:", rendered_steer)
        self.assertIn("Make timeout 5s", rendered_steer)

    def test_keyboard_controller_handle_key_and_posix_cbreak(self):
        """Verify keyboard controller key actions and terminal setup."""
        from unittest import mock
        from tui.keyboard import TerminalKeyboardController

        mock_engine = mock.MagicMock()
        mock_engine.is_paused = False
        ctrl = TerminalKeyboardController(engine=mock_engine, bus=self.bus, tui=self.tui)

        # Normal key handling
        self.assertEqual(ctrl.handle_key(" "), "paused")
        mock_engine.pause.assert_called_once()

        self.assertEqual(ctrl.handle_key("q"), "quit")
        mock_engine.request_stop.assert_called_once()

        # Paused typing handling
        mock_engine.is_paused = True
        self.assertEqual(ctrl.handle_key("H"), "typing")
        self.assertEqual(ctrl.handle_key("i"), "typing")
        self.assertEqual(self.tui.steering_buffer, "Hi")

        self.assertEqual(ctrl.handle_key("\r"), "steered")
        mock_engine.resume.assert_called()
        self.assertEqual(self.tui.steering_buffer, "")

        # Verify POSIX cbreak setup and cleanup without throwing
        mock_termios = mock.MagicMock()
        mock_tty = mock.MagicMock()
        mock_termios.tcgetattr.return_value = [1, 2, 3]
        mock_termios.TCSADRAIN = 1

        with mock.patch.dict("sys.modules", {"termios": mock_termios, "tty": mock_tty}), \
             mock.patch("os.name", "posix"), \
             mock.patch("sys.stdin.isatty", return_value=True):

            posix_ctrl = TerminalKeyboardController(engine=mock_engine, bus=self.bus)
            with posix_ctrl:
                mock_tty.setcbreak.assert_called_once()
            mock_termios.tcsetattr.assert_called_once()


if __name__ == "__main__":
    unittest.main()
