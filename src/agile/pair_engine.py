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
    """Orchestrates multi-model pair programming with Ping-Pong TDD and role rotation."""

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
        self._steering_guidance: List[str] = []

        # Wire operator steering listener
        self.bus.subscribe(EventType.OPERATOR_STEER, self._on_operator_steer)

    def _on_operator_steer(self, event: AgentEvent) -> None:
        """Capture operator steering guidance to inject into active prompts."""
        guidance = event.payload.get("guidance")
        if guidance:
            self._steering_guidance.append(guidance)

    def _wait_if_paused(self) -> None:
        """Block execution loop if paused by operator."""
        while self._paused:
            time.sleep(0.05)

    def pause(self) -> None:
        """Pause the pair programming engine (operator steering)."""
        self._paused = True
        self.bus.publish(AgentEvent(event_type=EventType.OPERATOR_PAUSE))

    def resume(self) -> None:
        """Resume execution."""
        self._paused = False
        self.bus.publish(AgentEvent(event_type=EventType.OPERATOR_RESUME))

    def run_sprint(self) -> bool:
        """Execute all stories in the sprint backlog sequentially."""
        self.bus.publish(
            AgentEvent(
                event_type=EventType.SPRINT_START,
                payload={"epic": self.backlog_mgr.sprint.epic_goal},
            )
        )

        while not self.backlog_mgr.sprint.is_complete:
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
        red_resp = self._call_model_and_dispatch_tools(test_model, red_prompt, PairRole.DRIVER)

        # -------------------------------------------------------------
        # 2. GREEN PHASE: Implementer writes code to turn tests green
        # -------------------------------------------------------------
        self._wait_if_paused()
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

        if self._steering_guidance:
            steering_notes = "\nOperator Steering Guidance:\n" + "\n".join(f"- {g}" for g in self._steering_guidance)
            green_prompt += f"\n\n{steering_notes}"
            self._steering_guidance.clear()

        green_resp = self._call_model_and_dispatch_tools(impl_model, green_prompt, PairRole.DRIVER)

        # -------------------------------------------------------------
        # 3. REVIEW PHASE: Test Author reviews Implementer's work
        # -------------------------------------------------------------
        self._wait_if_paused()
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
            f"Implementer ({impl_model}) response:\n{green_resp.content}\n\n"
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

        # -------------------------------------------------------------
        # 4. DEFINITION OF DONE VERIFICATION GATE
        # -------------------------------------------------------------
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
                },
            )
        )

        if dod.is_complete:
            self.bus.publish(
                AgentEvent(
                    event_type=EventType.STORY_DONE,
                    payload={"story_id": story.id, "title": story.title},
                )
            )
            return True

        return False

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
