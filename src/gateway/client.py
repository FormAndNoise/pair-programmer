"""Unified multi-provider Model Gateway for cross-vendor LLMs."""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.parse
from typing import Any, Dict, FrozenSet, List, Optional
import httpx
from agent_core.types import Message, Role, ToolCall, ToolDefinition

# Attempt importing litellm if installed
try:
    import litellm  # type: ignore
    _LITELLM_AVAILABLE = True
except ImportError:
    _LITELLM_AVAILABLE = False

DEFAULT_ALLOWED_OPENAI_HOSTS: FrozenSet[str] = frozenset(
    {
        "api.openai.com",
        "openrouter.ai",
        "api.groq.com",
    }
)

_REDACT_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{6,}"), "Bearer [REDACTED]"),
    (
        re.compile(
            r"(?i)(\b(?:x-api-key|x-goog-api-key|authorization|api[_-]?key)\s*[:=]\s*[\"']?)[A-Za-z0-9._\-+/=]{6,}([\"']?)"
        ),
        r"\1[REDACTED]\2",
    ),
    (
        re.compile(
            r"("
            r"sk-(?:ant-|or-v1-|proj-|live-)?[A-Za-z0-9_-]{8,}|"
            r"AIza[A-Za-z0-9_-]{8,}|"
            r"gh[pousr]_[A-Za-z0-9_]{8,}|"
            r"github_pat_[A-Za-z0-9_]{8,}|"
            r"AKIA[0-9A-Z]{16}|"
            r"ASIA[0-9A-Z]{16}|"
            r"xox[baprs]-[A-Za-z0-9-]{8,}|"
            r"gsk_[A-Za-z0-9_-]{8,}"
            r")"
        ),
        "[REDACTED]",
    ),
)


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

    def _total_attempts(self) -> int:
        """Return total attempts (>= 1) so max_retries <= 0 executes once without crashing."""
        return max(1, int(self.max_retries))

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        """Validate OpenAI-compatible base_url against official hosts unless explicitly opted in."""
        cleaned = (base_url or "").strip().rstrip("/")
        parsed = urllib.parse.urlparse(cleaned)
        host = (parsed.hostname or "").lower()
        if not parsed.scheme or not host:
            raise ValueError(f"Invalid OPENAI_BASE_URL '{base_url}': scheme and host are required.")
        if parsed.username or parsed.password:
            raise ValueError("Invalid OPENAI_BASE_URL: embedded credentials in URL are forbidden.")

        allow_custom = os.environ.get("ALLOW_CUSTOM_BASE_URL", "").strip().lower() in ("1", "true", "yes")
        if parsed.scheme == "https" and host in DEFAULT_ALLOWED_OPENAI_HOSTS:
            return cleaned
        if allow_custom and parsed.scheme in ("https", "http"):
            return cleaned
        raise ValueError(
            f"Refusing to send API credentials to untrusted OPENAI_BASE_URL '{base_url}' "
            f"(host '{host}' is not in {sorted(DEFAULT_ALLOWED_OPENAI_HOSTS)}). "
            "Set ALLOW_CUSTOM_BASE_URL=1 to explicitly allow custom or local endpoints."
        )

    def get_api_key(self, provider: str) -> Optional[str]:
        """Fetch API key from explicit config or environment variables."""
        normalized = provider.lower()
        env_map = {
            "anthropic": "ANTHROPIC_API_KEY",
            "claude": "ANTHROPIC_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "google": "GEMINI_API_KEY",
            "openai": "OPENAI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "groq": "GROQ_API_KEY",
        }
        key_var = env_map.get(normalized)
        lower_keys = {str(k).lower(): v for k, v in self.api_keys.items()}
        if normalized in lower_keys:
            return lower_keys[normalized]
        if key_var and key_var in os.environ:
            return os.environ[key_var]
        return None

    def require_api_key(self, provider: str, env_var: str) -> str:
        """Return the API key or raise a named error before any HTTP call."""
        key = self.get_api_key(provider)
        if not key:
            raise RuntimeError(
                f"Missing API key for provider '{provider}': "
                f"set {env_var} or configure ModelGateway(api_keys={{'{provider}': ...}}) "
                "before calling send_chat."
            )
        return key

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        """Return True only for retryable provider responses (429/5xx/timeout)."""
        return status_code == 429 or 500 <= status_code <= 599

    @staticmethod
    def _status_code_of(exc: Exception) -> Optional[int]:
        """Extract an HTTP status code from typed httpx/litellm exception attributes only."""
        for attr in ("status_code", "status", "code"):
            value = getattr(exc, attr, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        response = getattr(exc, "response", None)
        if response is not None:
            value = getattr(response, "status_code", None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    @staticmethod
    def _is_retryable_exception(exc: Exception) -> bool:
        """Classify an exception as retryable (429/5xx/timeout/network only)."""
        status = ModelGateway._status_code_of(exc)
        if status is not None:
            return ModelGateway._is_retryable_status(status)
        if isinstance(exc, (TimeoutError,)):
            return True
        name = type(exc).__name__.lower()
        if "timeout" in name or "connect" in name:
            return True
        return False

    @staticmethod
    def _redact(text: Any, limit: int = 500) -> str:
        """Redact known credential formats and authorization headers, then truncate."""
        raw = str(text)
        for pattern, replacement in _REDACT_PATTERNS:
            raw = pattern.sub(replacement, raw)
        return raw[:limit]

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        """Exponential backoff with jitter, capped to keep total wait bounded."""
        base = min(8.0, 2 ** attempt)
        return base + random.uniform(0, 0.5)

    def _should_retry(self, exc: Exception, attempt: int) -> bool:
        if attempt >= self._total_attempts() - 1:
            return False
        return self._is_retryable_exception(exc)

    def _sleep_before_retry(self, attempt: int) -> None:
        time.sleep(self._retry_delay(attempt))

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

        provider = model.split("/")[0].lower() if "/" in model else "openai"
        env_map = {
            "anthropic": "ANTHROPIC_API_KEY",
            "claude": "ANTHROPIC_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "google": "GEMINI_API_KEY",
            "openai": "OPENAI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "groq": "GROQ_API_KEY",
        }
        api_key = self.get_api_key(provider)
        litellm_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": formatted_messages,
            "tools": formatted_tools,
            "timeout": self.timeout,
        }
        if api_key:
            litellm_kwargs["api_key"] = api_key
        else:
            if provider in env_map:
                raise RuntimeError(
                    f"Missing API key for provider '{provider}': "
                    f"set {env_map[provider]} before calling send_chat."
                )
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url and provider in ("openai", "openrouter", "groq"):
            litellm_kwargs["base_url"] = self._validate_base_url(base_url)

        for attempt in range(self._total_attempts()):
            try:
                response = litellm.completion(**litellm_kwargs)
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
                if not self._should_retry(e, attempt):
                    raise RuntimeError(
                        f"LiteLLM call failed for model '{model}': {self._redact(e)}"
                    ) from e
                self._sleep_before_retry(attempt)

        raise RuntimeError(f"LiteLLM call failed for model '{model}'")

    def _send_via_direct_api(
        self,
        model: str,
        messages: List[Message],
        tools: Optional[List[ToolDefinition]] = None,
        system_prompt: Optional[str] = None,
    ) -> Message:
        """Fallback direct HTTP client for vendor endpoints without LiteLLM."""
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
        api_key = self.require_api_key("anthropic", "ANTHROPIC_API_KEY")
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        anthropic_messages: List[Dict[str, Any]] = []
        for msg in messages:
            converted = self._to_anthropic_message(msg)
            if (
                anthropic_messages
                and anthropic_messages[-1]["role"] == converted["role"] == "user"
            ):
                anthropic_messages[-1]["content"].extend(converted["content"])
            else:
                anthropic_messages.append(converted)

        payload: Dict[str, Any] = {
            "model": model_name,
            "max_tokens": 4096,
            "messages": anthropic_messages,
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
            for attempt in range(self._total_attempts()):
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
                    if not self._should_retry(e, attempt):
                        raise RuntimeError(
                            f"Anthropic API call failed for '{model_name}': {self._redact(e)}"
                        ) from e
                    self._sleep_before_retry(attempt)

        raise RuntimeError(f"Anthropic API call failed for '{model_name}'")

    def _send_via_gemini(
        self,
        model_name: str,
        messages: List[Message],
        tools: Optional[List[ToolDefinition]] = None,
        system_prompt: Optional[str] = None,
    ) -> Message:
        """Call the Google Gemini generateContent API directly without LiteLLM."""
        api_key = self.require_api_key("gemini", "GEMINI_API_KEY")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }

        gemini_contents: List[Dict[str, Any]] = []
        for msg in messages:
            converted = self._to_gemini_content(msg)
            if (
                gemini_contents
                and gemini_contents[-1]["role"] == converted["role"] == "user"
            ):
                gemini_contents[-1]["parts"].extend(converted["parts"])
            else:
                gemini_contents.append(converted)

        payload: Dict[str, Any] = {
            "contents": gemini_contents,
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
            for attempt in range(self._total_attempts()):
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
                    if not self._should_retry(e, attempt):
                        raise RuntimeError(
                            f"Gemini API call failed for '{model_name}': {self._redact(e)}"
                        ) from e
                    self._sleep_before_retry(attempt)

        raise RuntimeError(f"Gemini API call failed for '{model_name}'")

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
        env_var = {
            "openrouter": "OPENROUTER_API_KEY",
            "groq": "GROQ_API_KEY",
        }.get(provider.lower(), "OPENAI_API_KEY")
        api_key = self.require_api_key(provider, env_var)
        provider_base_urls = {
            "openrouter": "https://openrouter.ai/api/v1",
            "groq": "https://api.groq.com/openai/v1",
        }
        default_base_url = provider_base_urls.get(provider.lower(), "https://api.openai.com/v1")
        raw_base_url = os.environ.get("OPENAI_BASE_URL", default_base_url)
        base_url = self._validate_base_url(raw_base_url)

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
            for attempt in range(self._total_attempts()):
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
                    if not self._should_retry(e, attempt):
                        raise RuntimeError(
                            f"Direct API call failed for '{model_name}' at '{base_url}': {self._redact(e)}"
                        ) from e
                    self._sleep_before_retry(attempt)

        raise RuntimeError(f"Direct API call failed for '{model_name}' at '{base_url}'")
