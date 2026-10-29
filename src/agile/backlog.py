"""Agile Sprint Backlog manager with Human-Centered Outside-In Story validation."""

from __future__ import annotations

from typing import List, Optional
from agent_core.types import DefinitionOfDone, SprintBacklog, StoryStatus, UserStory
from sandbox.runner import SandboxRunner


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

    def verify_dod(
        self,
        story: UserStory,
        runner: SandboxRunner,
        test_command: str = "python -m unittest",
        cross_review_approved: bool = False,
        ux_verified: bool = True,
    ) -> DefinitionOfDone:
        """Evaluate the Definition of Done (DoD) quality gate."""
        # 1. Syntax check
        syntax_res = runner.run_command("python -m compileall -q .")
        syntax_valid = syntax_res.is_success

        # 2. Automated test run
        test_res = runner.run_command(test_command)
        tests_passed = test_res.is_success

        # 3. Update DoD status
        dod = DefinitionOfDone(
            syntax_valid=syntax_valid,
            tests_passed=tests_passed,
            cross_review_approved=cross_review_approved,
            user_experience_verified=ux_verified,
            notes=test_res.format_output() if not tests_passed else "All verification checks passed.",
        )

        story.dod = dod
        if dod.is_complete:
            story.status = StoryStatus.DONE

        return dod
