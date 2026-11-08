"""Automated conventional Git branching and merging for pair-programming sprints.

The GitManager listens to EventBus lifecycle events. It does not change the
engine. Each story gets a feature branch. Each TDD phase gets a Conventional
Commit that names the model that wrote the work. Stories that pass the
Definition of Done merge into the base branch with ``--no-ff``.

Security model
--------------
The Git metadata directory is kept OUTSIDE the sandbox workspace. Sandbox code
(local or Docker) can write any file in the workspace. If ``.git/`` were inside
it, sandbox code could plant hooks or config commands that host-side Git would
then run. Keeping the git dir outside the mount closes that escape path.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent_core.events import AgentEvent, EventBus, EventType
from sandbox.runner import build_sanitized_environment

MAX_COMMIT_FILE_BYTES = 5_000_000  # 5 MiB max per file in git history

# Files that sandbox runs generate or secret files that must never enter history.
DEFAULT_EXCLUDES: Tuple[str, ...] = (
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".coverage",
    ".tmp/",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "client_secret*.json",
    "service-account*.json",
)

_SECRET_CONTENT_PATTERN = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|"
    r"\bsk-ant-[A-Za-z0-9_-]{16,}\b|"
    r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}\b|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\bAIza[0-9A-Za-z_-]{35}\b"
)

_BASE_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

HARNESS_NAME = "Pair Programmer Harness"
HARNESS_EMAIL = "harness@pair-programmer.local"


class GitCommandError(RuntimeError):
    """Raised when a git subprocess returns a non-zero exit code."""


def slugify_branch_fragment(text: str, max_length: int = 40) -> str:
    """Convert free text into a safe, lowercase git ref fragment."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = slug[:max_length].rstrip("-")
    return slug or "story"


def model_identity(model: str) -> Tuple[str, str]:
    """Return a (name, email) author identity for a model identifier.

    Example: ``anthropic/claude-3-5-sonnet`` gives
    ``("claude-3-5-sonnet", "anthropic.claude-3-5-sonnet@pair-programmer.local")``.
    """
    clean = (model or "unknown-model").strip()
    name = clean.split("/")[-1] or clean
    local_part = re.sub(r"[^A-Za-z0-9._-]+", ".", clean).strip(".") or "model"
    return name, f"{local_part}@pair-programmer.local"


class GitManager:
    """Create story branches, attribute commits per model, and merge done stories.

    Parameters
    ----------
    workspace_dir:
        Sandbox workspace used as the git work tree.
    bus:
        EventBus to subscribe to. Pass None to drive the manager manually.
    git_dir:
        Location of the git metadata. Defaults to a sibling folder outside the
        workspace: ``<parent>/.pair_programmer_git/<workspace-name>.git``.
    base_branch:
        Integration branch that receives merged stories.
    delete_merged_branches:
        Delete story branches after a successful merge.
    git_binary:
        Path to the git executable.
    """

    def __init__(
        self,
        workspace_dir: str | Path,
        bus: Optional[EventBus] = None,
        git_dir: str | Path | None = None,
        base_branch: str = "main",
        delete_merged_branches: bool = False,
        git_binary: str = "git",
    ) -> None:
        self.workspace_dir = Path(workspace_dir).resolve()
        if (
            not isinstance(base_branch, str)
            or not _BASE_BRANCH_PATTERN.fullmatch(base_branch)
            or base_branch.startswith("-")
            or ".." in base_branch
        ):
            raise ValueError("base_branch must be a safe branch name without leading '-' or '..'.")
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        default_git_dir = self.workspace_dir.parent / ".pair_programmer_git" / f"{self.workspace_dir.name}.git"
        self.git_dir = Path(git_dir).resolve() if git_dir else default_git_dir
        self._assert_git_dir_outside_workspace()

        self.base_branch = base_branch
        self.delete_merged_branches = delete_merged_branches
        self.git_binary = git_binary
        self.bus = bus

        # Story context, keyed by story id.
        self._branches: Dict[str, str] = {}
        self._titles: Dict[str, str] = {}
        self._test_authors: Dict[str, str] = {}
        self._implementers: Dict[str, str] = {}
        self._active_story: Optional[str] = None
        self._last_status: Dict[str, str] = {}

        if bus is not None:
            self.attach(bus)

    # ------------------------------------------------------------------
    # EventBus wiring
    # ------------------------------------------------------------------

    def attach(self, bus: EventBus) -> None:
        """Subscribe the manager to sprint lifecycle events on the bus."""
        self.bus = bus
        bus.subscribe(EventType.SPRINT_START, self._on_sprint_start)
        bus.subscribe(EventType.STORY_START, self._on_story_start)
        bus.subscribe(EventType.STORY_STATUS_CHANGE, self._on_status_change)
        bus.subscribe(EventType.DOD_EVALUATION, self._on_dod_evaluation)
        bus.subscribe(EventType.STORY_DONE, self._on_story_done)

    @staticmethod
    def is_available(git_binary: str = "git") -> bool:
        """Return True when the git executable is on PATH."""
        return shutil.which(git_binary) is not None

    def _on_sprint_start(self, event: AgentEvent) -> None:
        self._guard(self.ensure_repository)

    def _on_story_start(self, event: AgentEvent) -> None:
        p = event.payload
        story_id = p.get("story_id", "US-UNKNOWN")
        self._titles[story_id] = p.get("title", story_id)
        self._test_authors[story_id] = p.get("test_author") or event.source_model or "unknown-model"
        self._implementers[story_id] = p.get("implementer") or "unknown-model"
        self._active_story = story_id
        self._guard(self.start_story_branch, story_id, self._titles[story_id])

    def _on_status_change(self, event: AgentEvent) -> None:
        story_id = event.payload.get("story_id")
        status = event.payload.get("status")
        prev_status = self._last_status.get(story_id)
        if not story_id or not status or prev_status == status:
            return
        self._last_status[story_id] = status
        title = self._titles.get(story_id, story_id)

        # Entering GREEN means the RED phase finished: commit the tests.
        if status == "GREEN_IMPL":
            self._guard(
                self.commit_phase, story_id, "test",
                f"add failing tests for {title}", self._test_authors.get(story_id),
            )
        # Entering REVIEW means the GREEN or REFACTOR phase finished: commit the code.
        elif status == "REVIEW":
            if prev_status == "REFACTOR":
                self._guard(
                    self.commit_phase, story_id, "fix",
                    f"refactor and heal {title}", self._implementers.get(story_id),
                )
            else:
                self._guard(
                    self.commit_phase, story_id, "feat",
                    f"implement {title}", self._implementers.get(story_id),
                )

    def _on_dod_evaluation(self, event: AgentEvent) -> None:
        story_id = event.payload.get("story_id")
        if not story_id or event.payload.get("is_complete"):
            return
        # DoD failed: keep the work on its branch and do not merge.
        self._guard(
            self.commit_phase, story_id, "chore",
            f"snapshot work that failed the Definition of Done for {self._titles.get(story_id, story_id)}",
            None,
        )
        self._emit(
            EventType.GIT_MERGE,
            story_id=story_id,
            branch=self._branches.get(story_id),
            merged=False,
            reason="Definition of Done not met. Branch left unmerged for operator review.",
        )

    def _on_story_done(self, event: AgentEvent) -> None:
        story_id = event.payload.get("story_id")
        if story_id:
            self._guard(self.merge_story, story_id)

    # ------------------------------------------------------------------
    # Git operations (also usable without the EventBus)
    # ------------------------------------------------------------------

    def ensure_repository(self) -> None:
        """Initialize the external git dir and the base branch if they are missing."""
        self._write_workspace_gitignore()
        if not (self.git_dir / "HEAD").exists():
            self.git_dir.parent.mkdir(parents=True, exist_ok=True)
            self._run_raw(["init", "--bare", "--quiet", str(self.git_dir)])
            self._git("config", "core.bare", "false")
            self._git("symbolic-ref", "HEAD", f"refs/heads/{self.base_branch}")
            self._write_excludes()
            self._git("add", "-A")
            self._commit("chore: initialize sprint workspace", author=None, allow_empty=True)
            self._emit(EventType.GIT_BRANCH, branch=self.base_branch, action="initialized",
                       git_dir=str(self.git_dir))
        else:
            self._write_excludes()

    def start_story_branch(self, story_id: str, title: str) -> str:
        """Create (or reuse) the feature branch for a story and check it out."""
        self.ensure_repository()
        branch = f"feat/{slugify_branch_fragment(story_id)}-{slugify_branch_fragment(title)}"
        self._branches[story_id] = branch
        self._titles.setdefault(story_id, title)

        # Carry any stray edits into the base branch first, so branches start clean.
        self._checkout(self.base_branch)
        self._git("add", "-A")
        self._commit("chore: capture workspace state before story", author=None, allow_empty=False)

        if self._branch_exists(branch):
            self._checkout(branch)
            action = "resumed"
        else:
            self._git("checkout", "--quiet", "-b", branch)
            action = "created"
        self._emit(EventType.GIT_BRANCH, story_id=story_id, branch=branch, action=action)
        return branch

    def commit_phase(self, story_id: str, commit_type: str, summary: str, author_model: Optional[str]) -> Optional[str]:
        """Stage all workspace changes and create one Conventional Commit.

        Returns the new commit hash, or None when nothing changed.
        """
        self.ensure_repository()
        branch = self._branches.get(story_id)
        if branch and self.current_branch() != branch:
            self._checkout(branch)

        scope = slugify_branch_fragment(story_id)
        message = f"{commit_type}({scope}): {summary}"
        if author_model:
            message += f"\n\nAuthored-by-model: {author_model}"

        self._git("add", "-A")
        sha = self._commit(message, author=author_model, allow_empty=False)
        if sha:
            self._emit(EventType.GIT_COMMIT, story_id=story_id, branch=branch, sha=sha,
                       message=message.splitlines()[0], author=author_model or HARNESS_NAME)
        return sha

    def merge_story(self, story_id: str) -> bool:
        """Merge a story branch into the base branch with a ``--no-ff`` merge commit."""
        branch = self._branches.get(story_id)
        if not branch:
            return False

        title = self._titles.get(story_id, story_id)
        self.commit_phase(story_id, "chore", f"finalize {title}", None)
        self._checkout(self.base_branch)

        scope = slugify_branch_fragment(story_id)
        message = (
            f"feat({scope}): deliver {title}\n\n"
            f"Merge branch '{branch}' after the Definition of Done passed.\n"
            f"Test author: {self._test_authors.get(story_id, 'unknown')}\n"
            f"Implementer: {self._implementers.get(story_id, 'unknown')}"
        )
        try:
            self._git("merge", "--no-ff", "--quiet", "-m", message, branch)
        except GitCommandError as exc:
            self._git_ok("merge", "--abort")
            self._checkout(branch)
            self._emit(EventType.GIT_MERGE, story_id=story_id, branch=branch, merged=False, reason=str(exc))
            return False

        sha = self.head_sha()
        if self.delete_merged_branches:
            self._git("branch", "--quiet", "-d", branch)
        self._emit(EventType.GIT_MERGE, story_id=story_id, branch=branch, merged=True,
                   into=self.base_branch, sha=sha)
        return True

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def head_sha(self) -> str:
        return self._git("rev-parse", "--short", "HEAD").strip()

    def log(self, ref: str = "HEAD", max_count: int = 50) -> List[Dict[str, str]]:
        """Return commits as dicts with ``sha``, ``author``, ``email``, and ``subject``."""
        out = self._git("log", "--topo-order", f"--max-count={max_count}", "--format=%h%x1f%an%x1f%ae%x1f%s", ref)
        entries = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                entries.append(dict(zip(("sha", "author", "email", "subject"), parts)))
        return entries

    def branch_for(self, story_id: str) -> Optional[str]:
        return self._branches.get(story_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _assert_git_dir_outside_workspace(self) -> None:
        try:
            self.git_dir.relative_to(self.workspace_dir)
        except ValueError:
            return
        raise ValueError(
            f"git_dir '{self.git_dir}' is inside the sandbox workspace. "
            "Sandbox code could tamper with hooks or config. Choose a path outside the workspace."
        )

    def _write_excludes(self) -> None:
        info = self.git_dir / "info"
        info.mkdir(parents=True, exist_ok=True)
        exclude_file = info / "exclude"
        existing_lines: List[str] = []
        if exclude_file.exists():
            try:
                existing_lines = exclude_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                existing_lines = []
        merged = list(existing_lines)
        for pat in DEFAULT_EXCLUDES:
            if pat not in merged:
                merged.append(pat)
        exclude_file.write_text("\n".join(merged) + "\n", encoding="utf-8")

    @staticmethod
    def _is_path_excluded(rel_posix_path: str) -> bool:
        parts = rel_posix_path.strip("/").split("/")
        basename = parts[-1] if parts else rel_posix_path
        for pattern in DEFAULT_EXCLUDES:
            clean_pat = pattern.rstrip("/")
            if pattern.endswith("/"):
                if any(fnmatch.fnmatchcase(p, clean_pat) for p in parts):
                    return True
            else:
                if fnmatch.fnmatchcase(basename, clean_pat) or fnmatch.fnmatchcase(rel_posix_path, clean_pat):
                    return True
        return False

    def _unstage_excluded_and_secret_files(self) -> None:
        """Unstage any file matching DEFAULT_EXCLUDES, size caps, or secret content signatures.

        Prevents a modified workspace .gitignore (e.g. ``!.env``) from forcing secrets
        into commit history.
        """
        try:
            staged_out = self._git("diff", "--cached", "--name-only")
        except GitCommandError:
            return
        staged_files = [line.strip() for line in staged_out.splitlines() if line.strip()]
        if not staged_files:
            return

        has_head = self._git_ok("rev-parse", "--verify", "--quiet", "HEAD")
        for rel_path in staged_files:
            rel_posix = rel_path.replace("\\", "/")
            should_unstage = self._is_path_excluded(rel_posix)
            full_path = self.workspace_dir / rel_path
            if not should_unstage and full_path.is_file():
                try:
                    st = full_path.stat()
                    if st.st_size > MAX_COMMIT_FILE_BYTES:
                        should_unstage = True
                    else:
                        sample = full_path.read_text(encoding="utf-8", errors="replace")
                        if _SECRET_CONTENT_PATTERN.search(sample):
                            should_unstage = True
                except OSError:
                    should_unstage = True

            if should_unstage:
                if has_head:
                    self._git_ok("reset", "-q", "HEAD", "--", rel_path)
                else:
                    self._git_ok("rm", "--cached", "-r", "-f", "--quiet", "--", rel_path)

    def _write_workspace_gitignore(self) -> None:
        """Add harness exclusions to the workspace ignore file without replacing it."""
        ignore_path = self.workspace_dir / ".gitignore"
        if ignore_path.is_symlink():
            ignore_path.unlink()
        existing = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
        block = ["# Pair Programmer harness exclusions", *DEFAULT_EXCLUDES]
        if existing.splitlines()[-len(block):] == block:
            return
        prefix = existing
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        content = prefix + "\n".join(block) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.workspace_dir,
            prefix=".pair-programmer-gitignore-",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        try:
            os.replace(temporary_path, ignore_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _branch_exists(self, branch: str) -> bool:
        return self._git_ok("show-ref", "--verify", "--quiet", f"refs/heads/{branch}")

    def _checkout(self, branch: str) -> None:
        if self.current_branch() != branch:
            self._git("checkout", "--quiet", branch)

    def _commit(self, message: str, author: Optional[str], allow_empty: bool) -> Optional[str]:
        self._unstage_excluded_and_secret_files()
        if not allow_empty and self._git_ok("diff", "--cached", "--quiet"):
            return None
        args = ["commit", "--quiet", "--no-verify", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        if author:
            name, email = model_identity(author)
            args += ["--author", f"{name} <{email}>"]
        self._git(*args)
        return self.head_sha()

    def _env(self) -> Dict[str, str]:
        env = build_sanitized_environment(os.environ, workspace_dir=self.workspace_dir)
        # Isolate from the operator's personal and system git config
        # (signing, hooks, aliases, autocrlf, ssh helpers) so runs are safe and reproducible.
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "echo"
        env["SSH_ASKPASS"] = "echo"
        env["GIT_AUTHOR_NAME"] = HARNESS_NAME
        env["GIT_AUTHOR_EMAIL"] = HARNESS_EMAIL
        env["GIT_COMMITTER_NAME"] = HARNESS_NAME
        env["GIT_COMMITTER_EMAIL"] = HARNESS_EMAIL
        return env

    def _run_raw(self, args: List[str]) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [self.git_binary, *args],
            capture_output=True, text=True, env=self._env(), timeout=60,
        )
        if proc.returncode != 0:
            raise GitCommandError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc

    def _base_args(self) -> List[str]:
        return [
            f"--git-dir={self.git_dir}",
            f"--work-tree={self.workspace_dir}",
            "-c", "core.hooksPath=" + os.devnull,
            "-c", "core.sshCommand=false",
            "-c", "protocol.allow=never",
            "-c", "commit.gpgsign=false",
            "-c", "core.autocrlf=false",
            "-c", "core.fsmonitor=false",
        ]

    def _git(self, *args: str) -> str:
        return self._run_raw([*self._base_args(), *args]).stdout

    def _git_ok(self, *args: str) -> bool:
        try:
            self._git(*args)
            return True
        except GitCommandError:
            return False

    def _guard(self, fn, *args) -> None:
        """Run a git operation without letting failures crash the sprint."""
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - report every failure as an event
            self._emit(EventType.ERROR, source="git_manager", error=f"Git automation failed: {exc}")

    def _emit(self, event_type: EventType, **payload) -> None:
        if self.bus is not None:
            self.bus.publish(AgentEvent(event_type=event_type, payload=payload))
