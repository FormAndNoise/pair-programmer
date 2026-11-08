"""Tests for Agile Backlog and Pair Programming Engine."""

import tempfile
import unittest
from pathlib import Path
from agent_core.events import EventBus, EventType
from agent_core.types import AgentConfig, Message, PairRole, Role, ToolCall, UserStory
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

    def test_multi_turn_tool_loop_feeds_tool_results_back_until_completion(self):
        """Multi-turn gateway loops tool observations back to the model until tool_calls is empty."""
        gateway = ScriptedMockGateway(multi_turn=True)
        backlog_mgr = BacklogManager.from_epic("Multi-Turn Calculator")
        story = backlog_mgr.add_story(
            title="Double Number",
            human_interaction_contract="`double(4)` returns `8`.",
            acceptance_criteria=["Handles positive integers"],
        )

        test_code = (
            "import unittest\n"
            "from calc import double\n"
            "class TestCalc(unittest.TestCase):\n"
            "    def test_double(self):\n"
            "        self.assertEqual(double(4), 8)\n"
            "if __name__ == '__main__': unittest.main()\n"
        )

        # RED (2 turns): write test file -> observe result -> finish
        gateway.queue_text_response(
            model="claude-3-5",
            content="Creating test file.",
            tool_calls=[ToolCall(id="red_1", name="write_file", arguments={"path": "test_calc.py", "content": test_code})],
        )
        gateway.queue_text_response(
            model="claude-3-5",
            content="Failing tests written.",
        )

        # GREEN (4 turns): list_dir -> read_file -> write_file -> finish
        gateway.queue_text_response(
            model="gemini-2-5",
            content="Listing workspace.",
            tool_calls=[ToolCall(id="g_1", name="list_dir", arguments={"path": "."})],
        )
        gateway.queue_text_response(
            model="gemini-2-5",
            content="Reading test file.",
            tool_calls=[ToolCall(id="g_2", name="read_file", arguments={"path": "test_calc.py"})],
        )
        gateway.queue_text_response(
            model="gemini-2-5",
            content="Writing implementation.",
            tool_calls=[ToolCall(id="g_3", name="write_file", arguments={"path": "calc.py", "content": "def double(x: int) -> int:\n    return x * 2\n"})],
        )
        gateway.queue_text_response(
            model="gemini-2-5",
            content="Implemented double() in calc.py.",
        )

        # REVIEW: approve
        gateway.queue_text_response(
            model="claude-3-5",
            content="Clean implementation. LGTM.",
        )

        engine = PairProgrammingEngine(
            config=self.config,
            backlog_mgr=backlog_mgr,
            runner=self.runner,
            registry=self.registry,
            gateway=gateway,
            bus=self.bus,
        )

        success = engine.execute_story_cycle(story)
        self.assertTrue(success)
        self.assertTrue(story.dod.is_complete)

        gemini_calls = [c for c in gateway.call_history if c["model"] == "gemini-2-5"]
        self.assertEqual(len(gemini_calls), 4)
        self.assertIn("`double(4)` returns `8`.", gemini_calls[0]["messages"][0].content)
        self.assertEqual(gemini_calls[1]["messages"][-1].role, Role.TOOL)
        self.assertIn("test_calc.py", gemini_calls[1]["messages"][-1].tool_result.output)
        self.assertEqual(gemini_calls[2]["messages"][-1].role, Role.TOOL)
        self.assertIn("self.assertEqual(double(4), 8)", gemini_calls[2]["messages"][-1].tool_result.output)

    def test_multi_turn_tool_loop_respects_max_turns_per_story_cap(self):
        """Runaway tool-calling loop halts cleanly at AgentConfig.max_turns_per_story."""
        class _RunawayGateway:
            def __init__(self):
                self.calls = 0

            def send_chat(self, model, messages, tools=None, system_prompt=None):
                self.calls += 1
                return Message(
                    role=Role.ASSISTANT,
                    content=f"Turn {self.calls}",
                    tool_calls=[ToolCall(id=f"c_{self.calls}", name="list_dir", arguments={"path": "."})],
                    model_name=model,
                )

        runaway_gw = _RunawayGateway()
        capped_config = AgentConfig(model_a="claude-3-5", model_b="gemini-2-5", max_turns_per_story=3)
        engine = PairProgrammingEngine(
            config=capped_config,
            backlog_mgr=BacklogManager.from_epic("Cap Test"),
            runner=self.runner,
            registry=self.registry,
            gateway=runaway_gw,
            bus=self.bus,
        )

        resp = engine._call_model_and_dispatch_tools("claude-3-5", "Explore", PairRole.DRIVER)
        self.assertEqual(runaway_gw.calls, 3)
        self.assertEqual(resp.content, "Turn 3")

    def test_review_prompt_includes_actual_workspace_files_and_contract(self):
        """Reviewer receives actual sandbox file contents and UX contract, not just implementer prose."""
        backlog_mgr = BacklogManager.from_epic("Review Visibility Epic")
        story = backlog_mgr.add_story(
            title="Triple Function",
            human_interaction_contract="`triple(3)` returns `9`.",
        )

        test_code = (
            "import unittest\n"
            "from tripler import triple\n"
            "class TestTriple(unittest.TestCase):\n"
            "    def test_triple(self): self.assertEqual(triple(3), 9)\n"
        )
        impl_code = "def triple(n: int) -> int:\n    return n * 3  # sentinel_impl_marker\n"

        self.mock_gateway.queue_text_response(
            model="claude-3-5",
            content="Wrote test.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": "test_tripler.py", "content": test_code})],
        )
        self.mock_gateway.queue_text_response(
            model="gemini-2-5",
            content="Done.",  # Minimal prose; omits actual code
            tool_calls=[ToolCall(name="write_file", arguments={"path": "tripler.py", "content": impl_code})],
        )
        self.mock_gateway.queue_text_response(
            model="claude-3-5",
            content="No issues found, LGTM!",
        )

        engine = PairProgrammingEngine(
            config=self.config,
            backlog_mgr=backlog_mgr,
            runner=self.runner,
            registry=self.registry,
            gateway=self.mock_gateway,
            bus=self.bus,
        )

        self.assertTrue(engine.execute_story_cycle(story))
        claude_calls = [c for c in self.mock_gateway.call_history if c["model"] == "claude-3-5"]
        review_prompt = claude_calls[1]["messages"][0].content
        self.assertIn("`triple(3)` returns `9`.", review_prompt)
        self.assertIn("tripler.py", review_prompt)
        self.assertIn("sentinel_impl_marker", review_prompt)

    def test_negated_lgtm_in_review_does_not_approve_story(self):
        """Negated LGTM phrases trigger rejection instead of false-positive approval."""
        config = AgentConfig(model_a="claude-3-5", model_b="gemini-2-5", max_refactor_attempts=1)
        engine = PairProgrammingEngine(
            config=config,
            backlog_mgr=BacklogManager.from_epic("Review Gate Epic"),
            runner=self.runner,
            registry=self.registry,
            gateway=self.mock_gateway,
            bus=self.bus,
        )
        story = engine.backlog_mgr.add_story(
            title="Check Negation",
            human_interaction_contract="`ok()` returns `True`.",
        )

        for critique in (
            "Not LGTM — missing input validation.",
            "I cannot give LGTM until edge cases pass.",
            "Do not mark LGTM yet.",
            "No LGTM: crashes on empty string.",
            "Almost LGTM, fix naming first.",
        ):
            self.mock_gateway.queue_text_response(model="claude-3-5", content=critique)
            _, approved = engine._run_review_phase(story, "claude-3-5", "gemini-2-5", "Implemented.")
            self.assertFalse(approved, f"Critique falsely approved: {critique!r}")

        for approval in (
            "LGTM",
            "No issues found, LGTM!",
            "Code is clean and defensive. lgtm.",
        ):
            self.mock_gateway.queue_text_response(model="claude-3-5", content=approval)
            _, approved = engine._run_review_phase(story, "claude-3-5", "gemini-2-5", "Implemented.")
            self.assertTrue(approved, f"Valid approval rejected: {approval!r}")

    def test_verify_dod_discovers_src_and_tests_layout_without_init_files(self):
        """verify_dod discovers tests in tests/ (without __init__.py) and resolves imports from src/."""
        (self.workspace / "src").mkdir()
        (self.workspace / "tests").mkdir()
        (self.workspace / "src" / "helpers.py").write_text(
            "def wrap_h1(text: str) -> str:\n    return f'<h1>{text}</h1>'\n",
            encoding="utf-8",
        )
        (self.workspace / "src" / "converter.py").write_text(
            "from helpers import wrap_h1\n"
            "def to_html(md: str) -> str:\n"
            "    return wrap_h1(md.lstrip('# ').strip())\n",
            encoding="utf-8",
        )

        backlog_mgr = BacklogManager.from_epic("Markdown Converter")
        story = backlog_mgr.add_story(
            title="H1 Heading",
            human_interaction_contract="`to_html('# Hi')` returns `'<h1>Hi</h1>'`.",
        )

        # 1. Failing test inside tests/ (no tests/__init__.py) MUST fail verify_dod
        (self.workspace / "tests" / "test_converter.py").write_text(
            "import unittest\n"
            "from converter import to_html\n"
            "class TestConverter(unittest.TestCase):\n"
            "    def test_h1(self):\n"
            "        self.assertEqual(to_html('# Hi'), '<h1>WRONG</h1>')\n",
            encoding="utf-8",
        )
        failing_dod = backlog_mgr.verify_dod(story, self.runner, cross_review_approved=True, require_ux_audit=True)
        self.assertFalse(failing_dod.tests_passed)
        self.assertFalse(failing_dod.is_complete)

        # 2. Passing test inside tests/ + sibling import inside src/ MUST pass all 4 DoD gates
        (self.workspace / "tests" / "test_converter.py").write_text(
            "import unittest\n"
            "from converter import to_html\n"
            "class TestConverter(unittest.TestCase):\n"
            "    def test_h1(self):\n"
            "        self.assertEqual(to_html('# Hi'), '<h1>Hi</h1>')\n",
            encoding="utf-8",
        )
        passing_dod = backlog_mgr.verify_dod(story, self.runner, cross_review_approved=True, require_ux_audit=True)
        self.assertTrue(passing_dod.syntax_valid)
        self.assertTrue(passing_dod.tests_passed, passing_dod.notes)
        self.assertTrue(passing_dod.user_experience_verified, passing_dod.notes)
        self.assertTrue(passing_dod.is_complete)

    def test_verify_dod_rejects_workspace_with_zero_discovered_tests(self):
        """verify_dod fails tests_passed when zero unit tests exist in the sandbox."""
        (self.workspace / "impl_only.py").write_text("def fn() -> int:\n    return 1\n", encoding="utf-8")
        backlog_mgr = BacklogManager.from_epic("Empty Test Suite Guard")
        story = backlog_mgr.add_story(
            title="Untested Function",
            human_interaction_contract="`fn()` returns `1`.",
        )
        dod = backlog_mgr.verify_dod(story, self.runner, cross_review_approved=True, require_ux_audit=True)
        self.assertFalse(dod.tests_passed)
        self.assertFalse(dod.is_complete)
        self.assertIn("No unit tests", dod.notes or "")


if __name__ == "__main__":
    unittest.main()
