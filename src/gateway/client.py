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
            "openrouter": "OPENROUTER_API_KEY",
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
        """Fallback direct HTTP client for vendor endpoints without LiteLLM."""
        # Detect provider from model string (e.g. 'openai/gpt-4o', 'gemini/gemini-2.5-flash', 'ollama/llama3')
        if "/" in model:
            provider, model_name = model.split("/", 1)
        else:
            provider = "openai"
            model_name = model

        normalized = provider.lower()
        if normalized in ("anthropic", "claude"):
            return self._send_via_anthropic(model_name, messages, tools, system_prompt)
        if normalized in ("gemini", "google"):
            return self._send_via_gemini(model_name, messages, tools, system_prompt)
        return self._send_via_openai_compatible(provider, model_name, messages, tools, system_prompt)

    def _send_via_anthropic(
        self,
        model_name: str,
        messages: List[Message],
        tools: Optional[List[ToolDefinition]] = None,
        system_prompt: Optional[str] = None,
    ) -> Message:
        """Call the Anthropic Messages API directly without LiteLLM."""
        api_key = self.get_api_key("anthropic") or ""
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        payload: Dict[str, Any] = {
            "model": model_name,
            "max_tokens": 4096,
            "messages": [self._to_anthropic_message(msg) for msg in messages],
        }
        if system_prompt:
            payload["system"] = system_prompt
        if tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters_schema,
                }
                for t in tools
            ]

        with httpx.Client(timeout=self.timeout) as client:
            for attempt in range(self.max_retries):
                try:
                    res = client.post(url, headers=headers, json=payload)
                    res.raise_for_status()
                    data = res.json()
                    content_parts = data.get("content", [])
                    text = "".join(
                        part.get("text", "") for part in content_parts if part.get("type") == "text"
                    )
                    tool_calls: List[ToolCall] = []
                    for part in content_parts:
                        if part.get("type") == "tool_use":
                            tool_calls.append(
                                ToolCall(
                                    id=part.get("id") or f"call_{len(tool_calls)}",
                                    name=part.get("name", ""),
                                    arguments=part.get("input") or {},
                                )
                            )
                    return Message(
                        role=Role.ASSISTANT,
                        content=text or None,
                        tool_calls=tool_calls if tool_calls else None,
                        model_name=f"anthropic/{model_name}",
                    )
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        raise RuntimeError(f"Anthropic API call failed for '{model_name}': {e}") from e
                    time.sleep(2 ** attempt)

        raise RuntimeError("Unreachable loop exit in _send_via_anthropic")

    def _send_via_gemini(
        self,
        model_name: str,
        messages: List[Message],
        tools: Optional[List[ToolDefinition]] = None,
        system_prompt: Optional[str] = None,
    ) -> Message:
        """Call the Google Gemini generateContent API directly without LiteLLM."""
        api_key = self.get_api_key("gemini") or ""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }

        payload: Dict[str, Any] = {
            "contents": [self._to_gemini_content(msg) for msg in messages],
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters_schema,
                        }
                        for t in tools
                    ]
                }
            ]

        with httpx.Client(timeout=self.timeout) as client:
            for attempt in range(self.max_retries):
                try:
                    res = client.post(url, headers=headers, json=payload)
                    res.raise_for_status()
                    data = res.json()
                    candidates = data.get("candidates", [])
                    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
                    text = "".join(part.get("text", "") for part in parts if "text" in part)
                    tool_calls = []
                    for part in parts:
                        function_call = part.get("functionCall")
                        if function_call:
                            tool_calls.append(
                                ToolCall(
                                    id=f"call_{len(tool_calls)}",
                                    name=function_call.get("name", ""),
                                    arguments=function_call.get("args") or {},
                                )
                            )
                    return Message(
                        role=Role.ASSISTANT,
                        content=text or None,
                        tool_calls=tool_calls if tool_calls else None,
                        model_name=f"gemini/{model_name}",
                    )
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        raise RuntimeError(f"Gemini API call failed for '{model_name}': {e}") from e
                    time.sleep(2 ** attempt)

        raise RuntimeError("Unreachable loop exit in _send_via_gemini")

    @staticmethod
    def _to_anthropic_message(msg: Message) -> Dict[str, Any]:
        """Convert a universal Message into the Anthropic Messages API format."""
        if msg.role == Role.TOOL and msg.tool_result is not None:
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_result.call_id,
                        "content": msg.tool_result.output,
                    }
                ],
            }
        blocks: List[Dict[str, Any]] = []
        if msg.content:
            blocks.append({"type": "text", "text": msg.content})
        for tool_call in msg.tool_calls or []:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "input": tool_call.arguments,
                }
            )
        if not blocks:
            blocks.append({"type": "text", "text": ""})
        role = "assistant" if msg.role == Role.ASSISTANT else "user"
        return {"role": role, "content": blocks}

    @staticmethod
    def _to_gemini_content(msg: Message) -> Dict[str, Any]:
        """Convert a universal Message into the Gemini generateContent format."""
        if msg.role == Role.TOOL and msg.tool_result is not None:
            return {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": msg.tool_result.name,
                            "response": {"result": msg.tool_result.output},
                        }
                    }
                ],
            }
        parts: List[Dict[str, Any]] = []
        if msg.content:
            parts.append({"text": msg.content})
        for tool_call in msg.tool_calls or []:
            parts.append({"functionCall": {"name": tool_call.name, "args": tool_call.arguments}})
        if not parts:
            parts.append({"text": ""})
        role = "model" if msg.role == Role.ASSISTANT else "user"
        return {"role": role, "parts": parts}

    def _send_via_openai_compatible(
        self,
        provider: str,
        model_name: str,
        messages: List[Message],
        tools: Optional[List[ToolDefinition]] = None,
        system_prompt: Optional[str] = None,
    ) -> Message:
        """Call any OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq, vLLM, Ollama)."""
        api_key = self.get_api_key(provider) or ""
        provider_base_urls = {
            "openrouter": "https://openrouter.ai/api/v1",
            "groq": "https://api.groq.com/openai/v1",
        }
        default_base_url = provider_base_urls.get(provider.lower(), "https://api.openai.com/v1")
        base_url = os.environ.get("OPENAI_BASE_URL", default_base_url)

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
                        model_name=model_name,
                    )
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        raise RuntimeError(f"Direct API call failed for '{model_name}' at '{base_url}': {e}") from e
                    time.sleep(2 ** attempt)

        raise RuntimeError("Unreachable loop exit in _send_via_openai_compatible")
