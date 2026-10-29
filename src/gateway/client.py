"""Unified multi-provider Model Gateway for cross-vendor LLMs."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional
import httpx
from agent_core.types import Message, Role, ToolCall, ToolDefinition

# Attempt importing litellm if installed
try:
    import litellm  # type: ignore
    _LITELLM_AVAILABLE = True
except ImportError:
    _LITELLM_AVAILABLE = False


class ModelGateway:
    """Unified client for invoking models across Anthropic, Gemini, OpenAI, and local LLMs."""

    def __init__(
        self,
        api_keys: Optional[Dict[str, str]] = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        self.api_keys = api_keys or {}
        self.timeout = timeout_seconds
        self.max_retries = max_retries

    def get_api_key(self, provider: str) -> Optional[str]:
        """Fetch API key from explicit config or environment variables."""
        env_map = {
            "anthropic": "ANTHROPIC_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "google": "GEMINI_API_KEY",
            "openai": "OPENAI_API_KEY",
        }
        key_var = env_map.get(provider.lower())
        if provider in self.api_keys:
            return self.api_keys[provider]
        if key_var and key_var in os.environ:
            return os.environ[key_var]
        return None

    def send_chat(
        self,
        model: str,
        messages: List[Message],
        tools: Optional[List[ToolDefinition]] = None,
        system_prompt: Optional[str] = None,
    ) -> Message:
        """Dispatch a chat completion request to the specified model."""
        if _LITELLM_AVAILABLE:
            return self._send_via_litellm(model, messages, tools, system_prompt)
        else:
            return self._send_via_direct_api(model, messages, tools, system_prompt)

    def _send_via_litellm(
        self,
        model: str,
        messages: List[Message],
        tools: Optional[List[ToolDefinition]] = None,
        system_prompt: Optional[str] = None,
    ) -> Message:
        """Call LLM via LiteLLM abstraction."""
        formatted_messages = []
        if system_prompt:
            formatted_messages.append({"role": "system", "content": system_prompt})

        for msg in messages:
            formatted_messages.append(msg.to_provider_format())

        formatted_tools = None
        if tools:
            formatted_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters_schema,
                    },
                }
                for t in tools
            ]

        for attempt in range(self.max_retries):
            try:
                response = litellm.completion(
                    model=model,
                    messages=formatted_messages,
                    tools=formatted_tools,
                    timeout=self.timeout,
                )
                choice = response.choices[0].message
                content = choice.content or ""
                tool_calls: List[ToolCall] = []

                if hasattr(choice, "tool_calls") and choice.tool_calls:
                    for tc in choice.tool_calls:
                        args = tc.function.arguments
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                args = {"raw": args}
                        tool_calls.append(
                            ToolCall(id=tc.id or f"call_{len(tool_calls)}", name=tc.function.name, arguments=args)
                        )

                return Message(
                    role=Role.ASSISTANT,
                    content=content,
                    tool_calls=tool_calls if tool_calls else None,
                    model_name=model,
                )
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise RuntimeError(f"LiteLLM call failed for model '{model}' after {self.max_retries} attempts: {e}")
                time.sleep(2 ** attempt)

        raise RuntimeError("Unreachable loop exit in _send_via_litellm")

    def _send_via_direct_api(
        self,
        model: str,
        messages: List[Message],
        tools: Optional[List[ToolDefinition]] = None,
        system_prompt: Optional[str] = None,
    ) -> Message:
        """Fallback direct HTTP client for OpenAI-compatible and vendor endpoints."""
        # Detect provider from model string (e.g. 'openai/gpt-4o', 'gemini/gemini-2.5-flash', 'ollama/llama3')
        if "/" in model:
            provider, model_name = model.split("/", 1)
        else:
            provider = "openai"
            model_name = model

        # OpenAI compatible route (works with OpenAI, OpenRouter, Groq, local vLLM/Ollama)
        api_key = self.get_api_key(provider) or ""
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        formatted_messages = []
        if system_prompt:
            formatted_messages.append({"role": "system", "content": system_prompt})
        for msg in messages:
            formatted_messages.append(msg.to_provider_format())

        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": formatted_messages,
        }

        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters_schema,
                    },
                }
                for t in tools
            ]

        with httpx.Client(timeout=self.timeout) as client:
            for attempt in range(self.max_retries):
                try:
                    res = client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
                    res.raise_for_status()
                    data = res.json()
                    choice = data["choices"][0]["message"]
                    content = choice.get("content") or ""
                    tool_calls = []

                    if "tool_calls" in choice and choice["tool_calls"]:
                        for tc in choice["tool_calls"]:
                            args_str = tc["function"]["arguments"]
                            try:
                                args = json.loads(args_str) if isinstance(args_str, str) else args_str
                            except Exception:
                                args = {"raw": args_str}
                            tool_calls.append(
                                ToolCall(id=tc.get("id", f"call_{len(tool_calls)}"), name=tc["function"]["name"], arguments=args)
                            )

                    return Message(
                        role=Role.ASSISTANT,
                        content=content,
                        tool_calls=tool_calls if tool_calls else None,
                        model_name=model,
                    )
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        raise RuntimeError(f"Direct API call failed for '{model}' at '{base_url}': {e}")
                    time.sleep(2 ** attempt)

        raise RuntimeError("Unreachable loop exit in _send_via_direct_api")
