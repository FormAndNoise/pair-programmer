"""XP Pair-Programming Engine coordinating dual cross-vendor models."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from agent_core.events import AgentEvent, EventBus, EventType
from agent_core.types import (
    AgentConfig,
    Message,
    PairRole,
    Role,
    StoryStatus,
    ToolCall,
    ToolDefinition,
    ToolResult,
    UserStory,
)
from gateway.client import ModelGateway
from sandbox.runner import SandboxRunner
from tools.registry import ToolRegistry
from .backlog import BacklogManager


class PairProgrammingEngine:
    """Orchestrates multi-model pair programming with Ping-Pong TDD, self-healing refactor loops, and role rotation."""

    def __init__(
        self,
        config: AgentConfig,
        backlog_mgr: BacklogManager,
        runner: SandboxRunner,
        registry: ToolRegistry,
        gateway: ModelGateway,
        bus: EventBus,
    ) -> None:
        self.config = config
        self.backlog_mgr = backlog_mgr
        self.runner = runner
        self.registry = registry
        self.gateway = gateway
        self.bus = bus
        self._paused = False
        self._stop_requested = False
        self._steering_guidance: List[str] = []

        # Wire operator steering and stop listeners
        self.bus.subscribe(EventType.OPERATOR_STEER, self._on_operator_steer)
        self.bus.subscribe(EventType.OPERATOR_STOP, self._on_operator_stop)

    @property
    def is_paused(self) -> bool:
        """Return True when the engine is paused by the operator."""
        return self._paused

    @property
    def stop_requested(self) -> bool:
        """Return True when the operator has requested a clean sprint exit."""
        return self._stop_requested

    def _on_operator_steer(self, event: AgentEvent) -> None:
        """Capture operator steering guidance to inject into active prompts."""
        guidance = event.payload.get("guidance")
        if guidance:
            self._steering_guidance.append(guidance)

    def _on_operator_stop(self, event: AgentEvent) -> None:
        """Handle operator stop event."""
        self._stop_requested = True
        self._paused = False

    def _wait_if_paused(self) -> None:
        """Block execution loop if paused by operator, exiting immediately on stop."""
        while self._paused and not self._stop_requested:
            time.sleep(0.05)

    def pause(self) -> None:
        """Pause the pair programming engine (operator steering)."""
        self._paused = True
        self.bus.publish(AgentEvent(event_type=EventType.OPERATOR_PAUSE))

    def resume(self) -> None:
        """Resume execution."""
        self._paused = False
        self.bus.publish(AgentEvent(event_type=EventType.OPERATOR_RESUME))

    def request_stop(self) -> None:
        """Signal the engine to stop cleanly at the next safe checkpoint."""
        self._stop_requested = True
        self._paused = False
        self.bus.publish(AgentEvent(event_type=EventType.OPERATOR_STOP))

    def _consume_steering_notes(self) -> str:
        """Drain and format any pending operator steering guidance."""
        if not self._steering_guidance:
            return ""
        notes = "\nOperator Steering Guidance:\n" + "\n".join(f"- {g}" for g in self._steering_guidance)
        self._steering_guidance.clear()
        return f"\n\n{notes}"

    def _can_attempt_refactor(self, impl_model: str) -> bool:
        """Return True when the gateway can execute another remediation turn."""
        if self._stop_requested:
            return False
        if hasattr(self.gateway, "has_queued_responses"):
            return bool(self.gateway.has_queued_responses(impl_model))
        return True

    def run_sprint(self) -> bool:
        """Execute all stories in the sprint backlog sequentially."""
        self.bus.publish(
            AgentEvent(
                event_type=EventType.SPRINT_START,
                payload={"epic": self.backlog_mgr.sprint.epic_goal},
            )
        )

        while not self.backlog_mgr.sprint.is_complete and not self._stop_requested:
            story = self.backlog_mgr.sprint.active_story
            if not story:
                break

            success = self.execute_story_cycle(story)
            if not success:
                self.bus.publish(
                    AgentEvent(
                        event_type=EventType.ERROR,
                        payload={"story_id": story.id, "error": "Story cycle failed to achieve DoD."},
                    )
                )
                return False

            self.backlog_mgr.sprint.advance()

        if self._stop_requested:
            return False

        self.bus.publish(
            AgentEvent(
                event_type=EventType.SPRINT_COMPLETE,
                payload={"epic": self.backlog_mgr.sprint.epic_goal},
            )
        )
        return True

    def execute_story_cycle(self, story: UserStory) -> bool:
        """Execute one complete Red-Green-Refactor Ping-Pong cycle on a story."""
        story_idx = self.backlog_mgr.sprint.current_index

        # Role Rotation across stories:
        # Story 0: Model A = Test Author, Model B = Implementer
        # Story 1: Model B = Test Author, Model A = Implementer
        if story_idx % 2 == 0:
            test_model = self.config.model_a
            impl_model = self.config.model_b
        else:
            test_model = self.config.model_b
            impl_model = self.config.model_a

        story.driver_model = test_model
        story.navigator_model = impl_model

        self.bus.publish(
            AgentEvent(
                event_type=EventType.STORY_START,
                source_model=test_model,
                payload={
                    "story_id": story.id,
                    "title": story.title,
                    "test_author": test_model,
                    "implementer": impl_model,
                    "ux_contract": story.human_interaction_contract,
                },
            )
        )

        # -------------------------------------------------------------
        # 1. RED PHASE: Test Author writes failing tests
        # -------------------------------------------------------------
        story.status = StoryStatus.RED_TESTS
        self.bus.publish(
            AgentEvent(
                event_type=EventType.STORY_STATUS_CHANGE,
                source_model=test_model,
                payload={"story_id": story.id, "status": "RED_TESTS"},
            )
        )

        red_prompt = (
            f"You are the Test Author ({test_model}).\n"
            f"User Story: {story.title}\n"
            f"Human Interaction & UX Contract: {story.human_interaction_contract}\n"
            f"Acceptance Criteria:\n" + "\n".join(f"- {c}" for c in story.acceptance_criteria) + "\n\n"
            "Task: Write comprehensive, failing unit tests using standard `unittest` to verify this feature. "
            "Use the `write_file` tool to save your test file in `tests/`."
        )

        self._wait_if_paused()
        if self._stop_requested:
            return False
        red_resp = self._call_model_and_dispatch_tools(test_model, red_prompt, PairRole.DRIVER)

        # -------------------------------------------------------------
        # 2. GREEN PHASE: Implementer writes code to turn tests green
        # -------------------------------------------------------------
        self._wait_if_paused()
        if self._stop_requested:
            return False
        story.status = StoryStatus.GREEN_IMPL
        self.bus.publish(
            AgentEvent(
                event_type=EventType.STORY_STATUS_CHANGE,
                source_model=impl_model,
                payload={"story_id": story.id, "status": "GREEN_IMPL"},
            )
        )

        green_prompt = (
            f"You are the Implementer ({impl_model}).\n"
            f"User Story: {story.title}\n"
            f"The Test Author ({test_model}) wrote tests with the following thought:\n{red_resp.content}\n\n"
            "Task: Implement the necessary code in `src/` to satisfy the tests and UX contract. "
            "Use `write_file` or `patch_file` to write the implementation."
        )
        green_prompt += self._consume_steering_notes()

        green_resp = self._call_model_and_dispatch_tools(impl_model, green_prompt, PairRole.DRIVER)

        # -------------------------------------------------------------
        # 3. REVIEW PHASE: Test Author reviews Implementer's work
        # -------------------------------------------------------------
        self._wait_if_paused()
        if self._stop_requested:
            return False
        review_resp, is_approved = self._run_review_phase(story, test_model, impl_model, green_resp.content)

        # -------------------------------------------------------------
        # 4. DEFINITION OF DONE & SELF-HEALING REFACTOR LOOP
        # -------------------------------------------------------------
        self._wait_if_paused()
        dod = self.backlog_mgr.verify_dod(
            story=story,
            runner=self.runner,
            cross_review_approved=is_approved,
            ux_verified=True,
        )

        refactor_attempt = 0
        max_attempts = max(0, self.config.max_refactor_attempts)

        while (
            not dod.is_complete
            and refactor_attempt < max_attempts
            and self._can_attempt_refactor(impl_model)
        ):
            refactor_attempt += 1
            self._wait_if_paused()
            if self._stop_requested:
                return False

            story.status = StoryStatus.REFACTOR
            self.bus.publish(
                AgentEvent(
                    event_type=EventType.STORY_STATUS_CHANGE,
                    source_model=impl_model,
                    payload={
                        "story_id": story.id,
                        "status": "REFACTOR",
                        "attempt": refactor_attempt,
                    },
                )
            )
            self.bus.publish(
                AgentEvent(
                    event_type=EventType.REFACTOR_ITERATION,
                    source_model=impl_model,
                    payload={
                        "story_id": story.id,
                        "attempt": refactor_attempt,
                        "max_attempts": max_attempts,
                        "syntax_valid": dod.syntax_valid,
                        "tests_passed": dod.tests_passed,
                        "cross_review_approved": dod.cross_review_approved,
                        "diagnostics": dod.notes or "",
                    },
                )
            )

            refactor_prompt = (
                f"You are the Implementer ({impl_model}) in the XP Refactor & Self-Healing loop "
                f"(Attempt {refactor_attempt}/{max_attempts}).\n"
                f"User Story: {story.title}\n"
                f"Human Interaction & UX Contract: {story.human_interaction_contract}\n\n"
                "The Definition of Done gate did not pass:\n"
                f"- Syntax Valid: {dod.syntax_valid}\n"
                f"- Sandbox Tests Passed: {dod.tests_passed}\n"
                f"- Reviewer Approved (LGTM): {dod.cross_review_approved}\n\n"
                f"Sandbox Verification Output:\n{dod.notes or 'No diagnostic output.'}\n\n"
                f"Reviewer ({test_model}) Critique:\n{review_resp.content or 'No critique provided.'}\n\n"
                "Task: Fix all failing tests, syntax errors, and reviewer concerns using `write_file` or `patch_file`."
            )
            refactor_prompt += self._consume_steering_notes()

            green_resp = self._call_model_and_dispatch_tools(impl_model, refactor_prompt, PairRole.DRIVER)

            self._wait_if_paused()
            if self._stop_requested:
                return False
            review_resp, is_approved = self._run_review_phase(story, test_model, impl_model, green_resp.content)

            self._wait_if_paused()
            dod = self.backlog_mgr.verify_dod(
                story=story,
                runner=self.runner,
                cross_review_approved=is_approved,
                ux_verified=True,
            )

        self.bus.publish(
            AgentEvent(
                event_type=EventType.DOD_EVALUATION,
                payload={
                    "story_id": story.id,
                    "is_complete": dod.is_complete,
                    "syntax_valid": dod.syntax_valid,
                    "tests_passed": dod.tests_passed,
                    "cross_review_approved": dod.cross_review_approved,
                    "user_experience_verified": dod.user_experience_verified,
                    "refactor_attempts": refactor_attempt,
                },
            )
        )

        if dod.is_complete:
            self.bus.publish(
                AgentEvent(
                    event_type=EventType.STORY_DONE,
                    payload={
                        "story_id": story.id,
                        "title": story.title,
                        "refactor_attempts": refactor_attempt,
                    },
                )
            )
            return True

        story.status = StoryStatus.BLOCKED
        return False

    def _run_review_phase(
        self,
        story: UserStory,
        test_model: str,
        impl_model: str,
        impl_content: Optional[str],
    ) -> tuple[Message, bool]:
        """Run the cross-model review step and return (review_message, is_approved)."""
        story.status = StoryStatus.REVIEW
        self.bus.publish(
            AgentEvent(
                event_type=EventType.STORY_STATUS_CHANGE,
                source_model=test_model,
                payload={"story_id": story.id, "status": "REVIEW"},
            )
        )

        review_prompt = (
            f"You are the Reviewer ({test_model}).\n"
            f"User Story: {story.title}\n"
            f"Implementer ({impl_model}) response:\n{impl_content}\n\n"
            "Task: Review the code for human usability, clarity, and defensive error handling. "
            "If it meets all quality bars, end your response with 'LGTM'. Otherwise, state required fixes."
        )

        review_resp = self.gateway.send_chat(
            model=test_model,
            messages=[Message(role=Role.USER, content=review_prompt)],
        )

        self.bus.publish(
            AgentEvent(
                event_type=EventType.CRITIQUE_RECEIVED,
                source_model=test_model,
                payload={"critique": review_resp.content or ""},
            )
        )

        is_approved = "LGTM" in (review_resp.content or "").upper()
        return review_resp, is_approved

    def _call_model_and_dispatch_tools(
        self,
        model: str,
        user_prompt: str,
        role: PairRole,
    ) -> Message:
        """Invoke a model, publish thinking, and execute returned tool calls."""
        messages = [Message(role=Role.USER, content=user_prompt)]
        tools = self.registry.get_definitions()

        resp = self.gateway.send_chat(model=model, messages=messages, tools=tools)
        resp.pair_role = role

        self.bus.publish(
            AgentEvent(
                event_type=EventType.MODEL_MESSAGE,
                source_model=model,
                payload={"content": resp.content, "role": role.value},
            )
        )

        if resp.tool_calls:
            for tc in resp.tool_calls:
                self.bus.publish(
                    AgentEvent(
                        event_type=EventType.TOOL_START,
                        source_model=model,
                        payload={"tool_name": tc.name, "arguments": tc.arguments},
                    )
                )

                result = self.registry.dispatch(tc.name, tc.arguments, call_id=tc.id)

                self.bus.publish(
                    AgentEvent(
                        event_type=EventType.TOOL_RESULT,
                        source_model=model,
                        payload={
                            "tool_name": tc.name,
                            "output": result.output,
                            "is_error": result.is_error,
                        },
                    )
                )

        return resp
