"""Visual TUI snapshot tests capturing SVG frames across the complete story lifecycle."""

import io
import unittest
from pathlib import Path
from rich.console import Console
from agent_core.events import AgentEvent, EventBus, EventType
from tui.app import PairProgrammingTUI


class TestTUISnapshots(unittest.TestCase):
    """Automates visual UX testing for the Phase 1 Rich Terminal User Interface."""

    def setUp(self) -> None:
        self.snapshots_dir = Path(__file__).parent / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def _render_and_export_snapshot(
        self,
        tui: PairProgrammingTUI,
        console: Console,
        filename: str,
        title: str,
    ) -> tuple[Path, str, str]:
        """Render layout snapshot, export as SVG and plain text, and save SVG artifact."""
        console.clear()
        tui.display_once()

        # Capture plain text without clearing recorded buffer
        plain_text = console.export_text(clear=False)
        # Capture SVG frame
        svg_content = console.export_svg(title=title, clear=True)

        out_path = self.snapshots_dir / filename
        out_path.write_text(svg_content, encoding="utf-8")

        return out_path, svg_content, plain_text

    def test_complete_sprint_lifecycle_svg_snapshots(self) -> None:
        """Simulate and capture SVG snapshots across 4 distinct lifecycle gates."""
        bus = EventBus()
        buf = io.StringIO()
        console = Console(file=buf, record=True, width=120, height=40, color_system="truecolor")
        tui = PairProgrammingTUI(bus=bus, console=console)

        # -------------------------------------------------------------
        # 1. SPRINT INITIALIZATION
        # -------------------------------------------------------------
        bus.publish(
            AgentEvent(
                event_type=EventType.SPRINT_START,
                payload={"epic": "Build Text Processing Suite"},
            )
        )
        svg_init, svg_content_init, plain_text_init = self._render_and_export_snapshot(
            tui, console, "01_sprint_init.svg", "Sprint Initialization (120x40)"
        )
        self.assertTrue(svg_init.exists())
        self.assertIn("<svg class=\"rich-terminal\"", svg_content_init)
        self.assertIn("Build Text Processing Suite", plain_text_init)
        self.assertIn("Sprint Backlog", plain_text_init)
        self.assertIn("CONTROLS:", plain_text_init)

        # -------------------------------------------------------------
        # 2. RED PHASE: Test Authoring
        # -------------------------------------------------------------
        bus.publish(
            AgentEvent(
                event_type=EventType.STORY_START,
                source_model="anthropic/claude-3-5-sonnet",
                payload={
                    "story_id": "US-01",
                    "title": "Word Frequency Counter",
                    "test_author": "anthropic/claude-3-5-sonnet",
                    "implementer": "gemini/gemini-2.5-flash",
                },
            )
        )
        bus.publish(
            AgentEvent(
                event_type=EventType.STORY_STATUS_CHANGE,
                source_model="anthropic/claude-3-5-sonnet",
                payload={"story_id": "US-01", "status": "RED_TESTS"},
            )
        )
        bus.publish(
            AgentEvent(
                event_type=EventType.MODEL_MESSAGE,
                source_model="anthropic/claude-3-5-sonnet",
                payload={
                    "role": "driver",
                    "content": "Writing failing unit tests with comprehensive assertions for word frequency counting.",
                },
            )
        )
        bus.publish(
            AgentEvent(
                event_type=EventType.TOOL_RESULT,
                payload={
                    "tool_name": "write_file",
                    "output": "Wrote 342 bytes to tests/unit/test_word_counter.py",
                },
            )
        )
        svg_red, svg_content_red, plain_text_red = self._render_and_export_snapshot(
            tui, console, "02_red_phase.svg", "Red Phase: Test Authoring (120x40)"
        )
        self.assertTrue(svg_red.exists())
        self.assertIn("<svg class=\"rich-terminal\"", svg_content_red)
        self.assertIn("Word Frequency Counter", plain_text_red)
        self.assertIn("RED_TESTS", plain_text_red)
        self.assertIn("anthropic", plain_text_red)

        # -------------------------------------------------------------
        # 3. GREEN PHASE: Implementation & Tool Invocation
        # -------------------------------------------------------------
        bus.publish(
            AgentEvent(
                event_type=EventType.STORY_STATUS_CHANGE,
                source_model="gemini/gemini-2.5-flash",
                payload={"story_id": "US-01", "status": "GREEN_IMPL"},
            )
        )
        bus.publish(
            AgentEvent(
                event_type=EventType.MODEL_MESSAGE,
                source_model="gemini/gemini-2.5-flash",
                payload={
                    "role": "driver",
                    "content": (
                        "from collections import Counter\n"
                        "def count_words(text: str) -> dict:\n"
                        "    if not text.strip(): return {}\n"
                        "    return dict(Counter(text.lower().split()))"
                    ),
                },
            )
        )
        bus.publish(
            AgentEvent(
                event_type=EventType.TOOL_RESULT,
                payload={
                    "tool_name": "write_file",
                    "output": "Successfully written 184 characters to 'word_counter.py'.",
                },
            )
        )
        svg_green, svg_content_green, plain_text_green = self._render_and_export_snapshot(
            tui, console, "03_green_phase.svg", "Green Phase: Implementation (120x40)"
        )
        self.assertTrue(svg_green.exists())
        self.assertIn("<svg class=\"rich-terminal\"", svg_content_green)
        self.assertIn("GREEN_IMPL", plain_text_green)
        self.assertIn("gemini", plain_text_green)
        self.assertIn("write_file", plain_text_green)

        # -------------------------------------------------------------
        # 4. REVIEW & DEFINITION OF DONE PASS
        # -------------------------------------------------------------
        bus.publish(
            AgentEvent(
                event_type=EventType.CRITIQUE_RECEIVED,
                source_model="anthropic/claude-3-5-sonnet",
                payload={"critique": "Implementation is clean, idiomatic, and defensive. LGTM."},
            )
        )
        bus.publish(
            AgentEvent(
                event_type=EventType.DOD_EVALUATION,
                payload={
                    "story_id": "US-01",
                    "syntax_valid": True,
                    "tests_passed": True,
                    "cross_review_approved": True,
                    "user_experience_verified": True,
                },
            )
        )
        bus.publish(
            AgentEvent(
                event_type=EventType.STORY_DONE,
                payload={"story_id": "US-01", "title": "Word Frequency Counter"},
            )
        )
        svg_dod, svg_content_dod, plain_text_dod = self._render_and_export_snapshot(
            tui, console, "04_dod_passed.svg", "DoD Quality Gate Complete (120x40)"
        )
        self.assertTrue(svg_dod.exists())
        self.assertIn("<svg class=\"rich-terminal\"", svg_content_dod)
        self.assertIn("DONE", plain_text_dod)
        self.assertIn("PASS", plain_text_dod)
        self.assertIn("LGTM", plain_text_dod)

    def test_compact_geometry_80x24_renders_without_exception(self) -> None:
        """Verify that standard legacy terminal geometry (80x24) renders cleanly."""
        bus = EventBus()
        compact_buf = io.StringIO()
        compact_console = Console(
            file=compact_buf,
            record=True,
            width=80,
            height=24,
            color_system="truecolor",
        )
        tui = PairProgrammingTUI(bus=bus, console=compact_console)

        bus.publish(AgentEvent(event_type=EventType.SPRINT_START, payload={"epic": "Compact Test"}))
        bus.publish(
            AgentEvent(event_type=EventType.STORY_START, payload={"story_id": "US-99", "title": "Compact Story"})
        )

        svg_compact, svg_content, plain_text = self._render_and_export_snapshot(
            tui, compact_console, "05_compact_80x24.svg", "Compact Geometry (80x24)"
        )

        self.assertTrue(svg_compact.exists())
        self.assertIn("<svg class=\"rich-terminal\"", svg_content)
        self.assertIn("Compact", plain_text)
        self.assertIn("Story", plain_text)


if __name__ == "__main__":
    unittest.main()
