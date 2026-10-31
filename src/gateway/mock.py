"""Deterministic scripted model gateway for zero-cost offline testing."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from agent_core.types import Message, Role, ToolCall, ToolDefinition


class ScriptedMockGateway:
    """Mock gateway that returns scripted responses for offline and unit tests."""

    def __init__(self) -> None:
        self._queues: Dict[str, List[Message]] = {}
        self.call_history: List[Dict[str, Any]] = []

    def queue_response(self, model: str, response: Message) -> None:
        """Queue a predetermined response for a specific model."""
        if model not in self._queues:
            self._queues[model] = []
        self._queues[model].append(response)

    def queue_text_response(
        self,
        model: str,
        content: str,
        tool_calls: Optional[List[ToolCall]] = None,
    ) -> None:
        """Helper to queue a simple assistant message."""
        msg = Message(
            role=Role.ASSISTANT,
            content=content,
            tool_calls=tool_calls,
            model_name=model,
        )
        self.queue_response(model, msg)

    def has_queued_responses(self, model: str) -> bool:
        """Return True when the mock queue has at least one pending response for model."""
        return bool(self._queues.get(model))

    def send_chat(
        self,
        model: str,
        messages: List[Message],
        tools: Optional[List[ToolDefinition]] = None,
        system_prompt: Optional[str] = None,
    ) -> Message:
        """Retrieve the next queued response for the given model."""
        self.call_history.append({
            "model": model,
            "messages": messages,
            "tools": tools,
            "system_prompt": system_prompt,
        })

        if model in self._queues and self._queues[model]:
            return self._queues[model].pop(0)

        # Fallback default response if queue is empty
        return Message(
            role=Role.ASSISTANT,
            content=f"[Mock Default] Acknowledged response from {model}.",
            model_name=model,
        )
