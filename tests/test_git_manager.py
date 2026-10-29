"""Phase 4: GitManager tests — conventional branching, model attribution, and merges."""

import tempfile
import unittest
from pathlib import Path

from agent_core.events import EventBus, EventType
from agent_core.types import AgentConfig, ToolCall
from agile.backlog import BacklogManager
from agile.pair_engine import PairProgrammingEngine
from gateway.mock import ScriptedMockGateway
from sandbox.git_manager import GitManager, model_identity, slugify_branch_fragment
from sandbox.runner import SandboxRunner
from tools.registry import ToolRegistry

GIT_AVAILABLE = GitManager.is_available()

MODEL_A = "anthropic/claude-3-5-sonnet"
MODEL_B = "gemini/gemini-2-5-flash"

TEST_WORD = (
    "import unittest\n"
    "from word_counter import count_words\n"
    "class TestWordCounter(unittest.TestCase):\n"
    "    def test_basic_count(self):\n"
    "        self.assertEqual(count_words('apple banana apple'), {'apple': 2, 'banana': 1})\n"
)
IMPL_WORD = (
    "from collections import Counter\n"
    "def count_words(text: str) -> dict:\n"
    "    return dict(Counter(text.lower().split()))\n"
)
TEST_SLUG = (
    "import unittest\n"
    "from slug_util import slugify\n"
    "class TestSlug(unittest.TestCase):\n"
    "    def test_slugify(self):\n"
    "        self.assertEqual(slugify('Hello World!'), 'hello-world')\n"
)
IMPL_SLUG = (
    "import re\n"
    "def slugify(text: str) -> str:\n"
    "    cleaned = re.sub(r'[^a-zA-Z0-9\\s]', '', text).strip().lower()\n"
    "    return re.sub(r'\\s+', '-', cleaned)\n"
)


class TestGitHelpers(unittest.TestCase):
    """Pure helper functions — always run."""

    def test_slugify_branch_fragment(self) -> None:
        self.assertEqual(slugify_branch_fragment("Word Frequency Counter!"), "word-frequency-counter")
        self.assertEqual(slugify_branch_fragment("US-229C"), "us-229c")
        self.assertEqual(slugify_branch_fragment("***"), "story")
        self.assertLessEqual(len(slugify_branch_fragment("x" * 200)), 40)

    def test_model_identity(self) -> None:
        name, email = model_identity(MODEL_A)
        self.assertEqual(name, "claude-3-5-sonnet")
        self.assertEqual(email, "anthropic.claude-3-5-sonnet@pair-programmer.local")

    def test_git_dir_inside_workspace_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                GitManager(td, git_dir=Path(td) / ".git")


@unittest.skipUnless(GIT_AVAILABLE, "git executable not available on this host.")
class TestGitManagerSprint(unittest.TestCase):
    """Drive real sprints through the engine with GitManager attached to the bus."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.workspace = root / "workspace"
        self.git_dir = root / "meta" / "workspace.git"
        self.bus = EventBus()
        self.runner = SandboxRunner(self.workspace)
        self.git = GitManager(self.workspace, bus=self.bus, git_dir=self.git_dir)
        self.gateway = ScriptedMockGateway()
        self.config = AgentConfig(model_a=MODEL_A, model_b=MODEL_B, sandbox_dir=str(self.workspace))
        self.backlog = BacklogManager.from_epic("Git Automation Sprint")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _engine(self) -> PairProgrammingEngine:
        return PairProgrammingEngine(
            config=self.config, backlog_mgr=self.backlog, runner=self.runner,
            registry=ToolRegistry(self.runner), gateway=self.gateway, bus=self.bus,
        )

    def _queue_story(self, test_model, impl_model, test_path, test_code, impl_path, impl_code, review="LGTM"):
        self.gateway.queue_text_response(
            model=test_model, content="Writing tests.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": test_path, "content": test_code})],
        )
        self.gateway.queue_text_response(
            model=impl_model, content="Implementing.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": impl_path, "content": impl_code})],
        )
        self.gateway.queue_text_response(model=test_model, content=review)

    def test_two_story_sprint_produces_branches_attributed_commits_and_merges(self) -> None:
        """Verify each story gets a branch, per-model commits, and a --no-ff merge into main."""
        s1 = self.backlog.add_story(title="Word Frequency Counter",
                                    human_interaction_contract="`count_words('a a')` returns `{'a': 2}`.")
        s2 = self.backlog.add_story(title="Slug Generator",
                                    human_interaction_contract="`slugify('Hi There')` returns `'hi-there'`.")
        self._queue_story(MODEL_A, MODEL_B, "test_word.py", TEST_WORD, "word_counter.py", IMPL_WORD)
        self._queue_story(MODEL_B, MODEL_A, "test_slug.py", TEST_SLUG, "slug_util.py", IMPL_SLUG)

        self.assertTrue(self._engine().run_sprint())

        # Metadata lives outside the workspace; the workspace has no .git entry.
        self.assertTrue((self.git_dir / "HEAD").exists())
        self.assertFalse((self.workspace / ".git").exists())
        self.assertEqual(self.git.current_branch(), "main")

        main_subjects = [c["subject"] for c in self.git.log("main")]
        scope1, scope2 = s1.id.lower(), s2.id.lower()
        self.assertIn(f"feat({scope1}): deliver Word Frequency Counter", main_subjects)
        self.assertIn(f"feat({scope2}): deliver Slug Generator", main_subjects)
        self.assertEqual(main_subjects[-1], "chore: initialize sprint workspace")

        # Story 1: Model A wrote tests, Model B implemented.
        log1 = {c["subject"]: c for c in self.git.log(self.git.branch_for(s1.id))}
        self.assertEqual(log1[f"test({scope1}): add failing tests for Word Frequency Counter"]["author"], "claude-3-5-sonnet")
        self.assertEqual(log1[f"feat({scope1}): implement Word Frequency Counter"]["author"], "gemini-2-5-flash")

        # Story 2: roles rotated.
        log2 = {c["subject"]: c for c in self.git.log(self.git.branch_for(s2.id))}
        self.assertEqual(log2[f"test({scope2}): add failing tests for Slug Generator"]["author"], "gemini-2-5-flash")
        self.assertEqual(log2[f"feat({scope2}): implement Slug Generator"]["author"], "claude-3-5-sonnet")

        # Branch naming follows feat/<story-id>-<slug>.
        self.assertEqual(self.git.branch_for(s1.id), f"feat/{scope1}-word-frequency-counter")

        # Bytecode from the DoD compile step is never tracked.
        tracked = self.git._git("ls-files")
        self.assertIn("word_counter.py", tracked)
        self.assertNotIn("__pycache__", tracked)

        merges = [e for e in self.bus.get_history() if e.event_type == EventType.GIT_MERGE]
        self.assertEqual(len(merges), 2)
        self.assertTrue(all(e.payload["merged"] for e in merges))

    def test_failed_dod_leaves_branch_unmerged(self) -> None:
        """Verify a story without reviewer approval stays on its branch and never reaches main."""
        story = self.backlog.add_story(title="Word Frequency Counter",
                                       human_interaction_contract="`count_words('a a')` returns `{'a': 2}`.")
        self._queue_story(MODEL_A, MODEL_B, "test_word.py", TEST_WORD, "word_counter.py", IMPL_WORD,
                          review="Needs fixes: add input validation.")

        self.assertFalse(self._engine().run_sprint())

        branch = self.git.branch_for(story.id)
        self.assertEqual(self.git.current_branch(), branch)
        main_files = self.git._git("ls-tree", "-r", "--name-only", "main")
        self.assertNotIn("word_counter.py", main_files)
        branch_files = self.git._git("ls-tree", "-r", "--name-only", branch)
        self.assertIn("word_counter.py", branch_files)

        merges = [e for e in self.bus.get_history() if e.event_type == EventType.GIT_MERGE]
        self.assertEqual(len(merges), 1)
        self.assertFalse(merges[0].payload["merged"])


@unittest.skipUnless(GIT_AVAILABLE, "git executable not available on this host.")
class TestGitManagerSecurity(unittest.TestCase):
    """Verify sandbox code cannot hijack host-side git."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.workspace = root / "workspace"
        self.marker = root / "HOOK_RAN"
        self.git = GitManager(self.workspace, git_dir=root / "meta.git")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_planted_workspace_git_hooks_do_not_execute(self) -> None:
        """Verify a hook written into workspace/.git by sandbox code is ignored on commit."""
        hooks = self.workspace / ".git" / "hooks"
        hooks.mkdir(parents=True)
        marker = self.marker.as_posix()
        for name in ("pre-commit", "post-commit", "commit-msg"):
            (hooks / name).write_text(f"#!/bin/sh\necho pwned > '{marker}'\n", encoding="utf-8")
        (self.workspace / ".git" / "config").write_text(
            f"[core]\n\thooksPath = {hooks.as_posix()}\n\tfsmonitor = echo pwned > '{marker}'\n", encoding="utf-8"
        )

        self.git.ensure_repository()
        self.git.start_story_branch("US-SEC1", "Security Check")
        (self.workspace / "module.py").write_text("x = 1\n", encoding="utf-8")
        sha = self.git.commit_phase("US-SEC1", "feat", "add module", MODEL_B)

        self.assertIsNotNone(sha)
        self.assertFalse(self.marker.exists(), "Planted hook or config command must not run on the host.")
        self.assertNotIn(".git/", self.git._git("ls-files"))

    def test_ensure_repository_is_idempotent(self) -> None:
        self.git.ensure_repository()
        first = self.git.log("main")
        self.git.ensure_repository()
        self.assertEqual(self.git.log("main"), first)

    def test_commit_with_no_changes_returns_none(self) -> None:
        self.git.start_story_branch("US-NOOP", "No Changes")
        self.assertIsNone(self.git.commit_phase("US-NOOP", "feat", "nothing", MODEL_A))


if __name__ == "__main__":
    unittest.main()
