"""XP Pair-Programming Engine coordinating dual cross-vendor models."""

from __future__ import annotations

import re
import threading
import time
from enum import Enum
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


class SprintOutcome(Enum):
    """Typed result of a sprint run so callers can distinguish outcomes."""

    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"

    @property
    def is_success(self) -> bool:
        return self is SprintOutcome.COMPLETED

    def __eq__(self, other: Any) -> bool:
        # Preserve truthiness/back-compat with the legacy bool return:
        # `outcome == True` / `assertTrue(outcome)` mean COMPLETED.
        if isinstance(other, bool):
            return self.is_success == other
        if isinstance(other, SprintOutcome):
            return self is other
        return NotImplemented

    def __bool__(self) -> bool:
        return self.is_success

    def __hash__(self) -> int:
        return hash(self.value)


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
        self._paused = threading.Event()
        self._stop_requested = threading.Event()
        self._state_lock = threading.Lock()
        self._steering_guidance: List[str] = []
        self._running = False

        # Wire operator steering and stop listeners
        self.bus.subscribe(EventType.OPERATOR_STEER, self._on_operator_steer)
        self.bus.subscribe(EventType.OPERATOR_STOP, self._on_operator_stop)

    @property
    def is_paused(self) -> bool:
        """Return True when the engine is paused by the operator."""
        return self._paused.is_set()

    @property
    def stop_requested(self) -> bool:
        """Return True when the operator has requested a clean sprint exit."""
        return self._stop_requested.is_set()

    @property
    def is_running(self) -> bool:
        """Return True while a sprint is executing on its engine thread."""
        with self._state_lock:
            return self._running

    def _set_running(self, value: bool) -> None:
        with self._state_lock:
            self._running = value

    def _on_operator_steer(self, event: AgentEvent) -> None:
        """Capture operator steering guidance to inject into active prompts."""
        guidance = event.payload.get("guidance")
        if guidance:
            with self._state_lock:
                self._steering_guidance.append(guidance)

    def _on_operator_stop(self, event: AgentEvent) -> None:
        """Handle operator stop event."""
        self._stop_requested.set()
        self._paused.clear()

    def _wait_if_paused(self) -> None:
        """Block execution loop if paused by operator, exiting immediately on stop.

        Uses Event.wait() so a stop signal wakes the loop promptly instead
        of busy-waiting with time.sleep().
        """
        while self._paused.is_set() and not self._stop_requested.is_set():
            self._paused.wait(timeout=0.05)

    def pause(self) -> None:
        """Pause the pair programming engine (operator steering)."""
        self._paused.set()
        self.bus.publish(AgentEvent(event_type=EventType.OPERATOR_PAUSE))

    def resume(self) -> None:
        """Resume execution."""
        self._paused.clear()
        self.bus.publish(AgentEvent(event_type=EventType.OPERATOR_RESUME))

    def request_stop(self) -> None:
        """Signal the engine to stop cleanly at the next safe checkpoint."""
        self._stop_requested.set()
        self._paused.clear()
        self.bus.publish(AgentEvent(event_type=EventType.OPERATOR_STOP))

    def reset_stop(self) -> None:
        """Clear the stop flag so a new sprint can start."""
        self._stop_requested.clear()

    def _consume_steering_notes(self) -> str:
        """Drain and format any pending operator steering guidance."""
        with self._state_lock:
            if not self._steering_guidance:
                return ""
            notes = "\nOperator Steering Guidance:\n" + "\n".join(
                f"- {g}" for g in self._steering_guidance
            )
            self._steering_guidance.clear()
        return f"\n\n{notes}"

    def _can_attempt_refactor(self, impl_model: str) -> bool:
        """Return True when the gateway can execute another remediation turn."""
        if self._stop_requested.is_set():
            return False
        if hasattr(self.gateway, "has_queued_responses"):
            return bool(self.gateway.has_queued_responses(impl_model))
        return True

    def run_sprint(self) -> SprintOutcome:
        """Execute all stories in the sprint backlog sequentially.

        Returns SprintOutcome.COMPLETED / FAILED / STOPPED. The enum keeps
        legacy bool semantics (COMPLETED == True, other outcomes == False).
        """
        self._set_running(True)
        try:
            self.bus.publish(
                AgentEvent(
                    event_type=EventType.SPRINT_START,
                    payload={"epic": self.backlog_mgr.sprint.epic_goal},
                )
            )

            while not self.backlog_mgr.sprint.is_complete and not self._stop_requested.is_set():
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
                    return SprintOutcome.FAILED

                self.backlog_mgr.sprint.advance()

            if self._stop_requested.is_set():
                return SprintOutcome.STOPPED

            self.bus.publish(
                AgentEvent(
                    event_type=EventType.SPRINT_COMPLETE,
                    payload={"epic": self.backlog_mgr.sprint.epic_goal},
                )
            )
            return SprintOutcome.COMPLETED
        finally:
            self._set_running(False)

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
        if self._stop_requested.is_set():
            return False
        red_resp = self._call_model_and_dispatch_tools(test_model, red_prompt, PairRole.DRIVER)
        story.pinned_test_hashes = self.backlog_mgr.snapshot_test_hashes(self.runner)

        # -------------------------------------------------------------
        # 2. GREEN PHASE: Implementer writes code to turn tests green
        # -------------------------------------------------------------
        self._wait_if_paused()
        if self._stop_requested.is_set():
            return False
        story.status = StoryStatus.GREEN_IMPL
        self.bus.publish(
            AgentEvent(
                event_type=EventType.STORY_STATUS_CHANGE,
                source_model=impl_model,
                payload={"story_id": story.id, "status": "GREEN_IMPL"},
            )
        )

        criteria_block = (
            "Acceptance Criteria:\n" + "\n".join(f"- {c}" for c in story.acceptance_criteria) + "\n"
            if story.acceptance_criteria
            else ""
        )
        green_prompt = (
            f"You are the Implementer ({impl_model}).\n"
            f"User Story: {story.title}\n"
            f"Human Interaction & UX Contract: {story.human_interaction_contract}\n"
            f"{criteria_block}"
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
        if self._stop_requested.is_set():
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
            require_ux_audit=self.config.require_ux_audit,
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
            if self._stop_requested.is_set():
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
                        "user_experience_verified": dod.user_experience_verified,
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
                f"- Reviewer Approved (LGTM): {dod.cross_review_approved}\n"
                f"- UX Contract Verified: {dod.user_experience_verified}\n\n"
                f"Sandbox Verification Output:\n{dod.notes or 'No diagnostic output.'}\n\n"
                f"Reviewer ({test_model}) Critique:\n{review_resp.content or 'No critique provided.'}\n\n"
                "Task: Fix all failing tests, syntax errors, and reviewer concerns using `write_file` or `patch_file`."
            )
            refactor_prompt += self._consume_steering_notes()

            green_resp = self._call_model_and_dispatch_tools(impl_model, refactor_prompt, PairRole.DRIVER)

            self._wait_if_paused()
            if self._stop_requested.is_set():
                return False
            review_resp, is_approved = self._run_review_phase(story, test_model, impl_model, green_resp.content)

            self._wait_if_paused()
            dod = self.backlog_mgr.verify_dod(
                story=story,
                runner=self.runner,
                cross_review_approved=is_approved,
                require_ux_audit=self.config.require_ux_audit,
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
            story.status = StoryStatus.DONE
            self.bus.publish(
                AgentEvent(
                    event_type=EventType.STORY_STATUS_CHANGE,
                    source_model=test_model,
                    payload={"story_id": story.id, "status": "DONE"},
                )
            )
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
        self.bus.publish(
            AgentEvent(
                event_type=EventType.STORY_STATUS_CHANGE,
                source_model=test_model,
                payload={"story_id": story.id, "status": "BLOCKED"},
            )
        )
        return False

    def _collect_workspace_snapshot(
        self,
        max_total_chars: int = 12000,
        max_file_chars: int = 4000,
    ) -> str:
        """Collect a bounded text snapshot of confined workspace files for review."""
        sections: List[str] = []
        used_chars = 0
        try:
            candidates = sorted(self.runner.workspace_dir.rglob("*"))
        except Exception:
            return "[Workspace unreadable]"

        for candidate in candidates:
            try:
                resolved = self.runner.resolve_path(candidate)
                if not resolved.is_file():
                    continue
                rel = resolved.relative_to(self.runner.workspace_dir)
                if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                    continue
                content = resolved.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            if len(content) > max_file_chars:
                content = content[:max_file_chars] + "\n... [truncated]"
            block = f"=== {rel.as_posix()} ===\n{content}"
            if used_chars + len(block) > max_total_chars:
                sections.append("... [remaining workspace files omitted]")
                break
            sections.append(block)
            used_chars += len(block)

        return "\n\n".join(sections) if sections else "[No workspace files present]"

    @staticmethod
    def _is_review_approved(content: Optional[str]) -> bool:
        """Return True when review text contains an un-negated standalone LGTM verdict."""
        text = content or ""
        if not re.search(r"\bLGTM\b", text, flags=re.IGNORECASE):
            return False
        if re.search(r"\b(?:not|no|never|almost|without|hardly|barely)\s+['\"`]*lgtm\b", text, flags=re.IGNORECASE):
            return False
        if re.search(
            r"\b(?:not|never|cannot|can't|won't|don't|do\s+not|didn't|isn't|unable\s+to|refuse\s+to|fail\s+to|hardly|barely)\b"
            r"[^.!?;,\n]{0,80}\blgtm\b",
            text,
            flags=re.IGNORECASE,
        ):
            return False
        if re.search(
            r"\blgtm\b[^.!?;,\n]{0,40}\b(?:but|except|however|unless|issue|fail|bug|error|problem)\b",
            text,
            flags=re.IGNORECASE,
        ):
            return False
        return True

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

        workspace_snapshot = self._collect_workspace_snapshot()
        review_prompt = (
            f"You are the Reviewer ({test_model}).\n"
            f"User Story: {story.title}\n"
            f"Human Interaction & UX Contract: {story.human_interaction_contract}\n"
            f"Implementer ({impl_model}) response:\n{impl_content}\n\n"
            f"Workspace Artifacts:\n{workspace_snapshot}\n\n"
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

        # Enforce that reviewer and implementer cannot be the same model,
        # and approval must come from test_model
        if test_model == impl_model:
            is_approved = False
        else:
            is_approved = self._is_review_approved(review_resp.content)
            if review_resp.model_name and review_resp.model_name != test_model:
                is_approved = False

        return review_resp, is_approved

    def _call_model_and_dispatch_tools(
        self,
        model: str,
        user_prompt: str,
        role: PairRole,
    ) -> Message:
        """Invoke a model in a bounded tool-execution loop until completion or turn limit."""
        messages: List[Message] = [Message(role=Role.USER, content=user_prompt)]
        tools = self.registry.get_definitions()
        max_turns = max(1, self.config.max_turns_per_story)
        last_resp: Optional[Message] = None

        for turn in range(max_turns):
            if turn > 0:
                self._wait_if_paused()
                if self._stop_requested.is_set() and last_resp is not None:
                    return last_resp

            resp = self.gateway.send_chat(model=model, messages=list(messages), tools=tools)
            resp.pair_role = role
            last_resp = resp

            self.bus.publish(
                AgentEvent(
                    event_type=EventType.MODEL_MESSAGE,
                    source_model=model,
                    payload={"content": resp.content, "role": role.value},
                )
            )

            if not resp.tool_calls:
                return resp

            messages.append(resp)
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
                messages.append(Message(role=Role.TOOL, content=result.output, tool_result=result))

            if getattr(self.gateway, "single_shot_tools", False):
                return resp

        assert last_resp is not None
        return last_resp
