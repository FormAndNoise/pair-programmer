"""Tests for Phase 5 capabilities:
1. Product Owner Autonomous Epic Decomposition (Option 2)
2. True XP Refactor & Self-Healing DoD Loop (Option 1)
3. Terminal Interactive Keyboard Controller (Option 3)
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from rich.console import Console

from agent_core.events import EventBus, EventType
from agent_core.types import AgentConfig, StoryStatus, ToolCall
from agile.backlog import BacklogManager
from agile.pair_engine import PairProgrammingEngine
from gateway.mock import ScriptedMockGateway
from sandbox.git_manager import GitManager
from sandbox.runner import SandboxRunner
from tools.registry import ToolRegistry
from tui.app import PairProgrammingTUI
from tui.keyboard import TerminalKeyboardController


class TestProductOwnerEpicDecomposition(unittest.TestCase):
    """Verify autonomous epic breakdown into Outside-In user stories."""

    def setUp(self) -> None:
        self.bus = EventBus()
        self.gateway = ScriptedMockGateway()
        self.console = Console(record=True, width=120, height=40)
        self.tui = PairProgrammingTUI(bus=self.bus, console=self.console)

    def test_decompose_epic_from_json_array_and_populate_tui(self) -> None:
        """Verify valid JSON array from Product Owner creates stories and updates TUI."""
        po_payload = json.dumps([
            {
                "title": "Parse Markdown Headers",
                "human_interaction_contract": "`parse_md('# Title')` returns `'<h1>Title</h1>'`.",
                "acceptance_criteria": ["Supports h1 to h3", "Strips leading hashes"],
            },
            {
                "title": "Render Inline Emphasis",
                "human_interaction_contract": "`parse_md('*bold*')` returns `'<em>bold</em>'`.",
                "acceptance_criteria": ["Handles single asterisks"],
            },
        ])
        self.gateway.queue_text_response(model="claude-3-5", content=po_payload)

        mgr = BacklogManager.from_epic_decomposed(
            epic_goal="Build Markdown to HTML Converter",
            gateway=self.gateway,
            model="claude-3-5",
            max_stories=4,
            bus=self.bus,
        )

        self.assertEqual(len(mgr.sprint.stories), 2)
        self.assertEqual(mgr.sprint.stories[0].title, "Parse Markdown Headers")
        self.assertEqual(mgr.sprint.stories[1].title, "Render Inline Emphasis")
        self.assertEqual(len(self.tui.stories), 2)
        self.assertEqual(self.tui.stories[0]["status"], "TODO")

    def test_decompose_epic_handles_fenced_json_and_max_stories_cap(self) -> None:
        """Verify markdown code fences and max_stories cap are respected."""
        fenced = (
            "Here is the sprint breakdown:\n```json\n"
            + json.dumps({
                "stories": [
                    {"title": "Story 1", "human_interaction_contract": "c1"},
                    {"title": "Story 2", "human_interaction_contract": "c2"},
                    {"title": "Story 3", "human_interaction_contract": "c3"},
                ]
            })
            + "\n```"
        )
        self.gateway.queue_text_response(model="claude-3-5", content=fenced)

        mgr = BacklogManager.from_epic("Three Story Epic")
        created = mgr.decompose_epic(gateway=self.gateway, model="claude-3-5", max_stories=2, bus=self.bus)

        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].title, "Story 1")
        self.assertEqual(created[1].title, "Story 2")

    def test_decompose_epic_falls_back_gracefully_on_invalid_json(self) -> None:
        """Verify unparseable model output produces a safe fallback story."""
        self.gateway.queue_text_response(model="claude-3-5", content="Sorry, I cannot return JSON right now.")

        mgr = BacklogManager.from_epic("Resilient Fallback Epic")
        created = mgr.decompose_epic(gateway=self.gateway, model="claude-3-5", max_stories=3, bus=self.bus)

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].title, "Initialize Feature & Spec")
        self.assertIn("Resilient Fallback Epic", created[0].human_interaction_contract)


class TestSelfHealingRefactorLoop(unittest.TestCase):
    """Verify the Red-Green-Refactor self-healing loop on test, syntax, or review failure."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = SandboxRunner(self.workspace)
        self.registry = ToolRegistry(self.runner)
        self.gateway = ScriptedMockGateway()
        self.bus = EventBus()
        self.config = AgentConfig(
            model_a="claude-3-5",
            model_b="gemini-2-5",
            max_refactor_attempts=2,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_self_healing_fixes_failing_unit_test_and_reviewer_rejection(self) -> None:
        """Verify story heals on Refactor Attempt 1 after initial buggy implementation."""
        backlog = BacklogManager.from_epic("Self-Healing Math Sprint")
        story = backlog.add_story(
            title="Safe Division",
            human_interaction_contract="`divide(10, 2)` returns `5.0`.",
        )

        test_code = (
            "import unittest\n"
            "from divider import divide\n"
            "class TestDiv(unittest.TestCase):\n"
            "    def test_div(self):\n"
            "        self.assertEqual(divide(10, 2), 5.0)\n"
            "if __name__ == '__main__': unittest.main()\n"
        )
        buggy_impl = "def divide(a: float, b: float) -> float:\n    return a * b  # Bug: multiplied instead of dividing\n"
        fixed_impl = "def divide(a: float, b: float) -> float:\n    return a / b\n"

        # 1. RED: Model A writes unit test
        self.gateway.queue_text_response(
            model="claude-3-5",
            content="Writing unit test for divide.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": "test_div.py", "content": test_code})],
        )

        # 2. GREEN (initial): Model B writes buggy code
        self.gateway.queue_text_response(
            model="gemini-2-5",
            content="Initial implementation of divide.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": "divider.py", "content": buggy_impl})],
        )

        # 3. REVIEW (initial): Model A rejects
        self.gateway.queue_text_response(
            model="claude-3-5",
            content="Bug detected: you used multiplication (`a * b`) instead of division (`a / b`). Please fix.",
        )

        # 4. REFACTOR (Attempt 1): Model B receives failure output + critique and fixes the bug
        self.gateway.queue_text_response(
            model="gemini-2-5",
            content="Fixed operator to division `a / b`.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": "divider.py", "content": fixed_impl})],
        )

        # 5. REVIEW (Attempt 1): Model A approves with LGTM
        self.gateway.queue_text_response(
            model="claude-3-5",
            content="Division logic is now accurate and tests pass. LGTM.",
        )

        engine = PairProgrammingEngine(
            config=self.config,
            backlog_mgr=backlog,
            runner=self.runner,
            registry=self.registry,
            gateway=self.gateway,
            bus=self.bus,
        )

        ok = engine.execute_story_cycle(story)
        self.assertTrue(ok)
        self.assertEqual(story.status, StoryStatus.DONE)
        self.assertTrue(story.dod.is_complete)

        # Verify REFACTOR_ITERATION event was emitted
        refactor_events = [e for e in self.bus.get_history() if e.event_type == EventType.REFACTOR_ITERATION]
        self.assertEqual(len(refactor_events), 1)
        self.assertEqual(refactor_events[0].payload["attempt"], 1)

        # Verify the refactor prompt fed the unit test failure traceback back to the implementer
        gemini_calls = [c for c in self.gateway.call_history if c["model"] == "gemini-2-5"]
        self.assertEqual(len(gemini_calls), 2)
        refactor_prompt_sent = gemini_calls[1]["messages"][0].content
        self.assertIn("AssertionError: 20 != 5.0", refactor_prompt_sent)
        self.assertIn("Bug detected: you used multiplication", refactor_prompt_sent)

    def test_exhausted_refactor_attempts_marks_story_blocked(self) -> None:
        """Verify story is marked BLOCKED when all refactor attempts fail."""
        backlog = BacklogManager.from_epic("Unhealing Sprint")
        story = backlog.add_story(
            title="Always Failing Feature",
            human_interaction_contract="`fn()` returns `True`.",
        )

        test_code = (
            "import unittest\n"
            "from broken import fn\n"
            "class TestBroken(unittest.TestCase):\n"
            "    def test_fn(self): self.assertTrue(fn())\n"
        )
        still_broken = "def fn(): return False\n"

        # RED
        self.gateway.queue_text_response(
            model="claude-3-5",
            content="Writing test.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": "test_broken.py", "content": test_code})],
        )
        # GREEN + 2 Refactor attempts all write False
        for _ in range(3):
            self.gateway.queue_text_response(
                model="gemini-2-5",
                content="Still returning False.",
                tool_calls=[ToolCall(name="write_file", arguments={"path": "broken.py", "content": still_broken})],
            )
            self.gateway.queue_text_response(
                model="claude-3-5",
                content="Still failing tests.",
            )

        engine = PairProgrammingEngine(
            config=self.config,
            backlog_mgr=backlog,
            runner=self.runner,
            registry=self.registry,
            gateway=self.gateway,
            bus=self.bus,
        )

        ok = engine.execute_story_cycle(story)
        self.assertFalse(ok)
        self.assertEqual(story.status, StoryStatus.BLOCKED)
        dod_events = [e for e in self.bus.get_history() if e.event_type == EventType.DOD_EVALUATION]
        self.assertEqual(len(dod_events), 1)
        self.assertEqual(dod_events[0].payload["refactor_attempts"], 2)


class TestTerminalKeyboardController(unittest.TestCase):
    """Verify non-blocking terminal keyboard controls for pause, inline steering, diff, and quit."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.runner = SandboxRunner(self.workspace)
        self.registry = ToolRegistry(self.runner)
        self.gateway = ScriptedMockGateway()
        self.bus = EventBus()
        self.console = Console(record=True, width=120, height=40)
        self.tui = PairProgrammingTUI(bus=self.bus, console=self.console)
        self.config = AgentConfig(model_a="claude-3-5", model_b="gemini-2-5")
        self.backlog = BacklogManager.from_epic("Keyboard Control Epic")
        self.backlog.add_story(title="Sample Story", human_interaction_contract="`f()` works.")
        self.engine = PairProgrammingEngine(
            config=self.config,
            backlog_mgr=self.backlog,
            runner=self.runner,
            registry=self.registry,
            gateway=self.gateway,
            bus=self.bus,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_space_pauses_and_inline_typing_submits_steering_on_enter(self) -> None:
        """Verify Space pauses, printable keys buffer with Backspace support, and Enter steers & resumes."""
        kb = TerminalKeyboardController(
            engine=self.engine,
            bus=self.bus,
            tui=self.tui,
            runner=self.runner,
        )

        # 1. Press Space to pause
        self.assertEqual(kb.handle_key(" "), "paused")
        self.assertTrue(self.engine.is_paused)
        self.assertTrue(self.tui.is_paused)

        # 2. Type "Use strictX" + Backspace + " types"
        for ch in "Use strictX":
            self.assertEqual(kb.handle_key(ch), "typing")
        self.assertEqual(kb.handle_key("\x08"), "typing")
        for ch in " types":
            self.assertEqual(kb.handle_key(ch), "typing")

        self.assertEqual(kb.steering_buffer, "Use strict types")
        self.assertEqual(self.tui.steering_buffer, "Use strict types")

        # 3. Press Enter to submit steering and resume
        self.assertEqual(kb.handle_key("\n"), "steered")
        self.assertFalse(self.engine.is_paused)
        self.assertEqual(kb.steering_buffer, "")
        self.assertIn("Use strict types", self.engine._steering_guidance)

    def test_space_twice_resumes_without_emitting_empty_steer(self) -> None:
        """Verify pressing Space then Space again resumes cleanly without empty steering events."""
        kb = TerminalKeyboardController(engine=self.engine, bus=self.bus, tui=self.tui, runner=self.runner)

        self.assertEqual(kb.handle_key(" "), "paused")
        self.assertTrue(self.engine.is_paused)
        self.assertEqual(kb.handle_key(" "), "resumed")
        self.assertFalse(self.engine.is_paused)

        steer_events = [e for e in self.bus.get_history() if e.event_type == EventType.OPERATOR_STEER]
        self.assertEqual(len(steer_events), 0)

    def test_diff_terminal_and_quit_keys(self) -> None:
        """Verify 'd', 'v', and 'q' keys trigger diff inspection, terminal status, and clean stop."""
        (self.workspace / "demo.py").write_text("x = 1\n", encoding="utf-8")
        kb = TerminalKeyboardController(engine=self.engine, bus=self.bus, tui=self.tui, runner=self.runner)

        self.assertEqual(kb.handle_key("d"), "diff")
        self.assertTrue(any("diff_inspect" in line for line in self.tui.vm_logs))

        self.assertEqual(kb.handle_key("v"), "terminal")
        self.assertTrue(any("terminal_view" in line for line in self.tui.vm_logs))

        self.assertEqual(kb.handle_key("q"), "quit")
        self.assertTrue(self.engine.stop_requested)

    def test_background_polling_thread_processes_synthetic_key_stream(self) -> None:
        """Verify the background thread polls key_reader and dispatches actions."""
        keys = [" ", "H", "i", "\r"]

        def fake_reader():
            return keys.pop(0) if keys else None

        kb = TerminalKeyboardController(
            engine=self.engine,
            bus=self.bus,
            tui=self.tui,
            runner=self.runner,
            key_reader=fake_reader,
            poll_interval=0.01,
        )
        with kb:
            deadline = time.time() + 1.0
            while keys and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.05)

        self.assertFalse(self.engine.is_paused)
        self.assertIn("Hi", self.engine._steering_guidance)


if __name__ == "__main__":
    unittest.main()
