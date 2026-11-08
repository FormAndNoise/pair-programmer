"""Decoupled asynchronous/synchronous Event Bus for UI, logging, and streaming."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional
from pydantic import BaseModel, Field
from agent_core.sanitize import strip_ansi


class EventType(str, Enum):
    # Sprint Lifecycle
    SPRINT_START = "sprint:start"
    SPRINT_COMPLETE = "sprint:complete"
    BACKLOG_DECOMPOSED = "sprint:backlog_decomposed"
    STORY_START = "story:start"
    STORY_STATUS_CHANGE = "story:status_change"
    REFACTOR_ITERATION = "story:refactor_iteration"
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
    OPERATOR_STOP = "operator:stop"

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
MAX_EVENT_HISTORY = 1000


class EventBus:
    """Thread-safe publish/subscribe bus for decoupling engine from UI/TUI."""

    def __init__(self) -> None:
        self._handlers: Dict[EventType, List[EventHandler]] = {}
        self._wildcard_handlers: List[EventHandler] = []
        self._history: Deque[AgentEvent] = deque(maxlen=MAX_EVENT_HISTORY)
        self._total_events = 0
        self._lock = threading.RLock()

    @staticmethod
    def _schedule_coroutine(res: Any) -> None:
        """Schedule a coroutine on the running loop, or close it if none is running.

        Never calls asyncio.create_task from a foreign thread, which would
        raise RuntimeError (no running loop in that thread).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            res.close()
            return
        loop.create_task(res)

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Subscribe to a specific event type."""
        with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe to all events (useful for loggers, UI event streams, WebSockets)."""
        with self._lock:
            self._wildcard_handlers.append(handler)

    def publish(self, event: AgentEvent) -> None:
        """Publish an event synchronously to all registered listeners."""
        with self._lock:
            self._history.append(event)
            self._total_events += 1
            handlers = list(self._handlers.get(event.event_type, []))
            wildcard_handlers = list(self._wildcard_handlers)

        def _dispatch(handler_list, context: str) -> None:
            for handler in handler_list:
                try:
                    res = handler(event)
                    if asyncio.iscoroutine(res):
                        self._schedule_coroutine(res)
                except Exception as e:
                    print(strip_ansi(f"[EventBus] Error in {context} handler {handler}: {e}"))

        _dispatch(handlers, "specific")
        _dispatch(wildcard_handlers, "wildcard")

    def get_history(self) -> List[AgentEvent]:
        """Return full event timeline."""
        with self._lock:
            return list(self._history)

    def get_total_event_count(self) -> int:
        """Return the number of events published since the last clear."""
        with self._lock:
            return self._total_events

    def clear(self) -> None:
        """Reset handlers and history."""
        with self._lock:
            self._handlers.clear()
            self._wildcard_handlers.clear()
            self._history.clear()
            self._total_events = 0
