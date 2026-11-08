"""Agile Sprint Backlog manager with Human-Centered Outside-In Story validation."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional
from agent_core.events import AgentEvent, EventBus, EventType
from agent_core.types import DefinitionOfDone, Message, Role, SprintBacklog, StoryStatus, UserStory
from sandbox.runner import SandboxRunner
from agile.ux_evaluator import OutsideInUXEvaluator, build_sandbox_python_env


class BacklogManager:
    """Manages sprint decomposition, story progression, and Definition of Done verification."""

    def __init__(self, sprint: SprintBacklog) -> None:
        self.sprint = sprint

    @classmethod
    def from_epic(
        cls,
        epic_goal: str,
        initial_stories: Optional[List[UserStory]] = None,
    ) -> BacklogManager:
        """Create a sprint backlog from an epic goal and initial user stories."""
        stories = initial_stories or []
        sprint = SprintBacklog(epic_goal=epic_goal, stories=stories)
        return cls(sprint)

    @classmethod
    def from_epic_decomposed(
        cls,
        epic_goal: str,
        gateway: Any,
        model: str,
        max_stories: int = 4,
        bus: Optional[EventBus] = None,
    ) -> BacklogManager:
        """Create a sprint backlog and decompose the epic with a Product Owner model."""
        mgr = cls.from_epic(epic_goal)
        mgr.decompose_epic(gateway=gateway, model=model, max_stories=max_stories, bus=bus)
        return mgr

    def add_story(
        self,
        title: str,
        human_interaction_contract: str,
        acceptance_criteria: Optional[List[str]] = None,
    ) -> UserStory:
        """Add a new atomic story defined with an Outside-In UX contract."""
        story = UserStory(
            title=title,
            human_interaction_contract=human_interaction_contract,
            acceptance_criteria=acceptance_criteria or [],
            status=StoryStatus.TODO,
        )
        self.sprint.stories.append(story)
        return story

    def decompose_epic(
        self,
        gateway: Any,
        model: str,
        max_stories: int = 4,
        bus: Optional[EventBus] = None,
    ) -> List[UserStory]:
        """Decompose the sprint epic into ordered Outside-In user stories via the model gateway.

        Falls back to a single default specification story if model output cannot be parsed.
        """
        limit = max(1, min(max_stories, 10))
        prompt = (
            f"You are the XP Product Owner.\n"
            f"Epic Goal: {self.sprint.epic_goal}\n\n"
            f"Decompose this epic into {limit} atomic, ordered Outside-In user stories.\n"
            "Every story MUST define a clear `human_interaction_contract` specifying "
            "exact function signatures, CLI behavior, or user-observable outputs.\n"
            "Respond ONLY with a valid JSON array of objects using this schema:\n"
            "[\n"
            "  {\n"
            '    "title": "Short story title",\n'
            '    "human_interaction_contract": "Exact callable or CLI contract",\n'
            '    "acceptance_criteria": ["Criterion 1", "Criterion 2"]\n'
            "  }\n"
            "]"
        )

        created: List[UserStory] = []
        try:
            resp = gateway.send_chat(
                model=model,
                messages=[Message(role=Role.USER, content=prompt)],
            )
            raw_text = (resp.content or "").strip()
            parsed_items = self._extract_story_specs(raw_text)
            for item in parsed_items[:limit]:
                title = str(item.get("title", "")).strip()
                contract = str(item.get("human_interaction_contract", "")).strip()
                if not title or not contract:
                    continue
                raw_criteria = item.get("acceptance_criteria") or []
                if isinstance(raw_criteria, list):
                    criteria = [str(c).strip() for c in raw_criteria if str(c).strip()]
                else:
                    criteria = [str(raw_criteria).strip()]
                created.append(
                    self.add_story(
                        title=title,
                        human_interaction_contract=contract,
                        acceptance_criteria=criteria,
                    )
                )
        except Exception:
            created = []

        if not created:
            fallback = self.add_story(
                title="Initialize Feature & Spec",
                human_interaction_contract=f"Implement basic interface and verification tests for: {self.sprint.epic_goal}",
                acceptance_criteria=["All unit tests pass", "Clean human-readable interface"],
            )
            created.append(fallback)

        if bus is not None:
            bus.publish(
                AgentEvent(
                    event_type=EventType.BACKLOG_DECOMPOSED,
                    source_model=model,
                    payload={
                        "epic": self.sprint.epic_goal,
                        "story_count": len(created),
                        "stories": [
                            {
                                "id": s.id,
                                "title": s.title,
                                "status": s.status.value.upper(),
                            }
                            for s in created
                        ],
                    },
                )
            )

        return created

    @staticmethod
    def _extract_story_specs(raw_text: str) -> List[ dict ]:
        """Extract a list of story dictionaries from raw model text or fenced JSON."""
        if not raw_text:
            return []

        # Strip optional markdown code fences
        fenced = re.search(r"```(?:json)?\s*(.*?)```", raw_text, flags=re.DOTALL | re.IGNORECASE)
        candidate = fenced.group(1).strip() if fenced else raw_text

        for snippet in (candidate, raw_text):
            try:
                data = json.loads(snippet)
                if isinstance(data, list):
                    return [d for d in data if isinstance(d, dict)]
                if isinstance(data, dict) and isinstance(data.get("stories"), list):
                    return [d for d in data["stories"] if isinstance(d, dict)]
            except Exception:
                pass

        # Attempt bracket extraction if prose surrounds the JSON array
        bracket_match = re.search(r"\[.*\]", raw_text, flags=re.DOTALL)
        if bracket_match:
            try:
                data = json.loads(bracket_match.group(0))
                if isinstance(data, list):
                    return [d for d in data if isinstance(d, dict)]
            except Exception:
                pass

        return []

    @staticmethod
    def snapshot_test_hashes(runner: SandboxRunner) -> Dict[str, str]:
        """Compute SHA256 hashes of all test files in the sandbox workspace."""
        hashes: Dict[str, str] = {}
        workspace = runner.workspace_dir
        tests_dir = workspace / "tests"
        if tests_dir.is_dir():
            for p in sorted(tests_dir.rglob("*.py")):
                if p.is_file():
                    rel = p.relative_to(workspace).as_posix()
                    hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(workspace.glob("test*.py")):
            if p.is_file():
                rel = p.relative_to(workspace).as_posix()
                hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        return hashes

    def verify_dod(
        self,
        story: UserStory,
        runner: SandboxRunner,
        test_command: str = "python -m unittest",
        cross_review_approved: bool = False,
        require_ux_audit: bool = True,
        require_docker_runner: bool = False,
    ) -> DefinitionOfDone:
        """Evaluate the Definition of Done (DoD) quality gate.

        Runs the quality gates in order: docker runner verification (if required),
        harness smoke test, syntax check, sandbox test suite (with test pinning
        integrity validation), cross-model review, and Outside-In UX contract audit.
        """
        failure_notes: List[str] = []

        # 0. Docker runner verification if demanded
        if require_docker_runner:
            is_docker = runner.__class__.__name__ == "DockerSandboxRunner"
            if not is_docker:
                failure_notes.append("Definition of Done requires execution inside DockerSandboxRunner.")
                dod = DefinitionOfDone(
                    syntax_valid=False,
                    tests_passed=False,
                    cross_review_approved=cross_review_approved,
                    user_experience_verified=False,
                    notes="\n\n".join(failure_notes),
                )
                story.dod = dod
                return dod

        # 1. Harness-owned smoke test outside agent-writable tests/
        py_env = build_sandbox_python_env(runner)
        smoke_res = runner.run_command("python -c \"import sys, unittest; sys.exit(0)\"", trusted_env=py_env)
        smoke_ok = smoke_res.is_success
        if not smoke_ok:
            failure_notes.append(f"Harness environment smoke check failed:\n{smoke_res.format_output()}")

        # 2. Syntax check
        syntax_res = runner.run_command("python -m compileall -q .", trusted_env=py_env)
        syntax_valid = syntax_res.is_success and smoke_ok
        if not syntax_valid:
            failure_notes.append(f"Syntax check failed:\n{syntax_res.format_output()}")

        # 3. Test pinning integrity check (assert tests were not tampered with post-RED)
        test_tampered = False
        if story.pinned_test_hashes is not None:
            current_hashes = self.snapshot_test_hashes(runner)
            if current_hashes != story.pinned_test_hashes:
                test_tampered = True
                failure_notes.append(
                    "Test suite integrity check failed: tests were modified, added, or deleted after the RED phase."
                )

        # 4. Automated test run (supporting both tests/ + src/ and flat workspace layouts)
        tests_dir = runner.workspace_dir / "tests"
        has_tests_dir = tests_dir.is_dir() and any(tests_dir.rglob("test*.py"))
        has_root_tests = any(p.is_file() for p in runner.workspace_dir.glob("test*.py"))

        if test_command == "python -m unittest" and has_tests_dir and not has_root_tests:
            test_res = runner.run_command("python -m unittest discover -s tests", trusted_env=py_env)
        elif (
            test_command == "python -m unittest"
            and has_tests_dir
            and has_root_tests
            and not (tests_dir / "__init__.py").exists()
        ):
            root_res = runner.run_command("python -m unittest", trusted_env=py_env)
            sub_res = runner.run_command("python -m unittest discover -s tests", trusted_env=py_env)
            test_res = root_res if not root_res.is_success else sub_res
        else:
            test_res = runner.run_command(test_command, trusted_env=py_env)

        no_tests_ran = "Ran 0 tests" in f"{test_res.stdout}\n{test_res.stderr}"
        tests_passed = test_res.is_success and not no_tests_ran and not test_tampered and smoke_ok

        if no_tests_ran:
            failure_notes.append(
                f"Test suite failed: No unit tests were discovered or executed.\n{test_res.format_output()}"
            )
        elif not test_res.is_success:
            failure_notes.append(f"Test suite failed:\n{test_res.format_output()}")

        # 5. Outside-In UX contract audit (the real fourth gate)
        ux_verified = True
        if require_ux_audit:
            scorecard = OutsideInUXEvaluator(runner).evaluate_story_contract(story)
            ux_verified = scorecard.passed
            if not ux_verified:
                failure_notes.append(f"UX contract not satisfied:\n{scorecard.feedback}")

        notes = "\n\n".join(failure_notes) if failure_notes else "All verification checks passed."

        # 6. Update DoD status
        dod = DefinitionOfDone(
            syntax_valid=syntax_valid,
            tests_passed=tests_passed,
            cross_review_approved=cross_review_approved,
            user_experience_verified=ux_verified,
            notes=notes,
        )

        story.dod = dod
        if dod.is_complete:
            story.status = StoryStatus.DONE

        return dod
