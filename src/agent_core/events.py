"""Decoupled asynchronous/synchronous Event Bus for UI, logging, and streaming."""

from __future__ import annotations

import asyncio
import time
import uuid
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from pydantic import BaseModel, Field


class EventType(str, Enum):
    # Sprint Lifecycle
    SPRINT_START = "sprint:start"
    SPRINT_COMPLETE = "sprint:complete"
    STORY_START = "story:start"
    STORY_STATUS_CHANGE = "story:status_change"
    STORY_DONE = "story:done"

    # Multi-Model Collaboration
    PAIR_ROLE_SWAP = "pair:role_swap"
    MODEL_THINKING = "model:thinking"
    MODEL_CHUNK = "model:chunk"
    MODEL_MESSAGE = "model:message"
    CRITIQUE_RECEIVED = "pair:critique_received"

    # Tool & Sandbox Execution
    TOOL_START = "tool:start"
    TOOL_RESULT = "tool:result"
    SANDBOX_COMMAND = "sandbox:command"

    # Quality Gate & DoD
    DOD_EVALUATION = "dod:evaluation"

    # Version Control (Phase 4 GitManager)
    GIT_BRANCH = "git:branch"
    GIT_COMMIT = "git:commit"
    GIT_MERGE = "git:merge"

    # Operator Steering & Control
    OPERATOR_PAUSE = "operator:pause"
    OPERATOR_STEER = "operator:steer"
    OPERATOR_RESUME = "operator:resume"

    # Diagnostics
    ERROR = "system:error"


class AgentEvent(BaseModel):
    """Structured event emitted during agent runtime."""
    id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:8]}")
    event_type: EventType
    timestamp: float = Field(default_factory=time.time)
    source_model: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


EventHandler = Callable[[AgentEvent], Any]


class EventBus:
    """Thread-safe publish/subscribe bus for decoupling engine from UI/TUI."""

    def __init__(self) -> None:
        self._handlers: Dict[EventType, List[EventHandler]] = {}
        self._wildcard_handlers: List[EventHandler] = []
        self._history: List[AgentEvent] = []

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Subscribe to a specific event type."""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe to all events (useful for loggers, UI event streams, WebSockets)."""
        self._wildcard_handlers.append(handler)

    def publish(self, event: AgentEvent) -> None:
        """Publish an event synchronously to all registered listeners."""
        self._history.append(event)

        # Notify specific listeners
        for handler in self._handlers.get(event.event_type, []):
            try:
                res = handler(event)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                print(f"[EventBus] Error in handler {handler}: {e}")

        # Notify wildcard listeners
        for handler in self._wildcard_handlers:
            try:
                res = handler(event)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                print(f"[EventBus] Error in wildcard handler: {e}")

    def get_history(self) -> List[AgentEvent]:
        """Return full event timeline."""
        return list(self._history)

    def clear(self) -> None:
        """Reset handlers and history."""
        self._handlers.clear()
        self._wildcard_handlers.clear()
        self._history.clear()
