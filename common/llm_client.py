"""Provider-neutral, redacting chat client for shared LLM calls."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
import json
import logging
import os
import time
from typing import Any, Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.redaction import redact, redact_value

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_REQUESTS_PER_MINUTE = 10.0
DEFAULT_RETRIES = 2
MAX_BACKOFF_SECONDS = 8.0

# In-memory prompt cache for LLM_CACHE=1
_LLM_CACHE_STORE: dict[str, Any] = {}


class LLMUnavailable(RuntimeError):
    """Raised when LLM calls have been intentionally disabled or cannot be made."""


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: str = "user"
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)

    @property
    def type(self) -> str:
        return self.role


# Compatibility message classes for drop-in replacement of message types
class SystemMessage(Message):
    role: str = "system"

    def __init__(self, content: str = "", **kwargs: Any) -> None:
        super().__init__(role="system", content=content, **kwargs)


class HumanMessage(Message):
    role: str = "user"

    def __init__(self, content: str = "", **kwargs: Any) -> None:
        super().__init__(role="user", content=content, **kwargs)


class AIMessage(Message):
    role: str = "assistant"

    def __init__(self, content: str = "", **kwargs: Any) -> None:
        super().__init__(role="assistant", content=content, **kwargs)


class ToolMessage(Message):
    role: str = "tool"
    tool_call_id: str | None = None

    def __init__(self, content: str = "", tool_call_id: str | None = None, **kwargs: Any) -> None:
        super().__init__(role="tool", content=content, **kwargs)
        self.tool_call_id = tool_call_id


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    model: str = ""


def _redact_value(value: Any) -> Any:
    if isinstance(value, Message):
        return Message(
            role=value.role,
            content=_redact_value(value.content),
            tool_calls=[
                ToolCall(
                    id=tc.id,
                    name=tc.name,
                    args={k: _redact_value(v) for k, v in tc.args.items()},
                )
                for tc in value.tool_calls
            ],
        )
    if isinstance(value, Mapping):
        return {
            str(key): _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_value(item) for item in value]
    return redact_value(value)


def _serialized_messages(messages: Sequence[Any]) -> str:
    safe: list[Any] = []
    for msg in messages:
        if isinstance(msg, Message):
            safe.append({"role": msg.role, "content": _redact_value(msg.content)})
        elif isinstance(msg, Mapping):
            safe.append({str(k): _redact_value(v) for k, v in msg.items()})
        else:
            safe.append({"role": "user", "content": _redact_value(str(msg))})
    return json.dumps(safe, default=str, sort_keys=True, ensure_ascii=True)


def _message_hash_and_length(messages: Sequence[Any]) -> tuple[str, int]:
    content = _serialized_messages(messages)
    return sha256(content.encode("utf-8")).hexdigest(), len(content)


def _is_retryable(error: BaseException) -> bool:
    status = getattr(error, "code", None) or getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int) and (status == 429 or 500 <= status <= 599):
        return True
    error_str = str(error).lower()
    return "429" in error_str or "quota" in error_str or "rate limit" in error_str


class TokenBucketRateLimiter:
    """Async token bucket limiting the average request rate over each minute."""

    def __init__(
        self,
        requests_per_minute: float = DEFAULT_REQUESTS_PER_MINUTE,
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self.capacity = max(1.0, requests_per_minute)
        self.tokens = self.capacity
        self.refill_per_second = requests_per_minute / 60.0
        self._sleep = sleep
        self._clock = clock
        self._updated_at = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = self._clock()
                elapsed = max(0.0, now - self._updated_at)
                self.tokens = min(
                    self.capacity, self.tokens + elapsed * self.refill_per_second
                )
                self._updated_at = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait_seconds = (1.0 - self.tokens) / self.refill_per_second
            await self._sleep(wait_seconds)


class LLMClient:
    """Redacting chat wrapper using google-genai directly with retries and rate limiting."""

    def __init__(
        self,
        model: Any = None,
        *,
        api_key: str | None = None,
        model_name: str | None = None,
        provider: str | None = None,
        timeout_seconds: float | None = None,
        requests_per_minute: float | None = None,
        offline: bool | None = None,
        max_retries: int = DEFAULT_RETRIES,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.getenv("LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
        )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")

        rpm_setting = os.getenv("LLM_REQUESTS_PER_MINUTE")
        configured_rpm = (
            requests_per_minute
            if requests_per_minute is not None
            else float(rpm_setting) if rpm_setting else DEFAULT_REQUESTS_PER_MINUTE
        )
        self.rate_limiter = TokenBucketRateLimiter(configured_rpm, sleep=sleep)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._sleep = sleep
        self.offline = (
            offline if offline is not None else os.getenv("LLM_OFFLINE", "0") == "1"
        )
        self.provider = (provider or os.getenv("LLM_PROVIDER", "gemini")).strip().lower()
        self.model_name = model_name or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.use_cache = os.getenv("LLM_CACHE", "0") == "1"

        self._genai_client: Any = None
        if self.offline or model is not None:
            self.model = model
            return

        if self.provider != "gemini":
            raise ValueError("LLM_PROVIDER must be 'gemini'")
        configured_key = api_key or os.getenv("GEMINI_API_KEY")
        if not configured_key:
            raise ValueError("GEMINI_API_KEY must be configured for the Gemini provider")

        from google import genai
        self._genai_client = genai.Client(api_key=configured_key)
        self.model = None

    async def _call_gemini_raw(
        self,
        contents: list[str],
        temperature: float = 0.0,
        response_schema: type[BaseModel] | None = None,
    ) -> tuple[str, dict[str, int]]:
        from google.genai import types

        config = types.GenerateContentConfig(
            temperature=temperature,
        )
        if response_schema is not None:
            config.response_mime_type = "application/json"
            config.response_schema = response_schema

        response = await asyncio.to_thread(
            self._genai_client.models.generate_content,
            model=self.model_name,
            contents=contents,
            config=config,
        )

        text = response.text or ""
        usage = {}
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            usage = {
                "prompt_tokens": getattr(response.usage_metadata, "prompt_token_count", 0) or 0,
                "candidates_tokens": getattr(response.usage_metadata, "candidates_token_count", 0) or 0,
            }
        return text, usage

    def bind_tools(self, tools: Sequence[Any]) -> "_BoundLLMClient":
        """Return an async adapter compatible with framework tool-binding APIs."""
        return _BoundLLMClient(self, tools)

    async def chat(
        self,
        messages: Sequence[Any],
        tools: Sequence[Any] | None = None,
        response_schema: type[BaseModel] | Mapping[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> Any:
        """Send redacted messages and return response or parsed Pydantic schema."""
        if self.offline:
            raise LLMUnavailable("LLM calls are disabled by LLM_OFFLINE=1")
        if not 0.0 <= temperature <= 2.0:
            raise ValueError("temperature must be between 0 and 2")

        safe_messages = _redact_value(messages)
        prompt_hash, prompt_length = _message_hash_and_length(safe_messages)

        # Cache check
        if self.use_cache and prompt_hash in _LLM_CACHE_STORE:
            return _LLM_CACHE_STORE[prompt_hash]

        started = time.perf_counter()
        completed = False
        usage_info: dict[str, int] = {}

        # Prepare text contents for generation
        contents: list[str] = []
        for msg in safe_messages:
            if isinstance(msg, Message):
                contents.append(f"{msg.role}: {msg.content}")
            elif isinstance(msg, Mapping):
                contents.append(f"{msg.get('role', 'user')}: {msg.get('content', '')}")
            else:
                contents.append(str(msg))

        try:
            for attempt in range(self.max_retries + 1):
                await self.rate_limiter.acquire()
                try:
                    if self.model is not None:
                        # Custom / mock model invoke
                        if hasattr(self.model, "ainvoke"):
                            bound_model = (
                                self.model.bind_tools(list(tools))
                                if tools and hasattr(self.model, "bind_tools")
                                else self.model
                            )
                            raw_resp = await asyncio.wait_for(
                                bound_model.ainvoke(safe_messages),
                                timeout=self.timeout_seconds,
                            )
                        elif callable(self.model):
                            raw_resp = await asyncio.wait_for(
                                asyncio.to_thread(self.model, safe_messages),
                                timeout=self.timeout_seconds,
                            )
                        else:
                            raw_resp = self.model

                        parsed_by_model = (
                            raw_resp.get("parsed")
                            if isinstance(raw_resp, Mapping)
                            else None
                        )
                        raw_message = (
                            raw_resp.get("raw")
                            if isinstance(raw_resp, Mapping)
                            else raw_resp
                        )
                        text = getattr(raw_message, "content", str(raw_message))
                        usage_info = {}
                    else:
                        parsed_by_model = None
                        text, usage_info = await asyncio.wait_for(
                            self._call_gemini_raw(
                                contents,
                                temperature=temperature,
                                response_schema=response_schema if isinstance(response_schema, type) and issubclass(response_schema, BaseModel) else None,
                            ),
                            timeout=self.timeout_seconds,
                        )

                    completed = True

                    if response_schema is None:
                        resp_obj = AIMessage(content=text)
                        if self.use_cache:
                            _LLM_CACHE_STORE[prompt_hash] = resp_obj
                        return resp_obj

                    # Handle structured schema parsing
                    if isinstance(response_schema, type) and issubclass(response_schema, BaseModel):
                        if isinstance(parsed_by_model, response_schema):
                            if self.use_cache:
                                _LLM_CACHE_STORE[prompt_hash] = parsed_by_model
                            return parsed_by_model
                        try:
                            # Clean possible markdown code fences
                            clean_text = text.strip()
                            if clean_text.startswith("```json"):
                                clean_text = clean_text[7:]
                            if clean_text.startswith("```"):
                                clean_text = clean_text[3:]
                            if clean_text.endswith("```"):
                                clean_text = clean_text[:-3]
                            parsed = response_schema.model_validate_json(clean_text.strip())
                            if self.use_cache:
                                _LLM_CACHE_STORE[prompt_hash] = parsed
                            return parsed
                        except ValidationError:
                            # Single repair retry
                            repair_prompt = [
                                *safe_messages,
                                AIMessage(content=text),
                                HumanMessage(
                                    content=(
                                        "Your previous response did not match the required JSON schema. "
                                        "Return a corrected response only, with valid JSON conforming to the schema."
                                    )
                                ),
                            ]
                            await self.rate_limiter.acquire()
                            if self.model is not None and hasattr(self.model, "ainvoke"):
                                repaired_response = await asyncio.wait_for(
                                    self.model.ainvoke(_redact_value(repair_prompt)),
                                    timeout=self.timeout_seconds,
                                )
                                repaired_parsed = (
                                    repaired_response.get("parsed")
                                    if isinstance(repaired_response, Mapping)
                                    else None
                                )
                                if isinstance(repaired_parsed, response_schema):
                                    if self.use_cache:
                                        _LLM_CACHE_STORE[prompt_hash] = repaired_parsed
                                    return repaired_parsed
                                repaired_raw = (
                                    repaired_response.get("raw")
                                    if isinstance(repaired_response, Mapping)
                                    else repaired_response
                                )
                                repaired_text = getattr(repaired_raw, "content", str(repaired_raw))
                            else:
                                repaired_text, _ = await asyncio.wait_for(
                                    self._call_gemini_raw(
                                        contents
                                        + [
                                            f"assistant: {text}",
                                            "user: Correct the response to match the required JSON schema.",
                                        ],
                                        temperature=0.0,
                                        response_schema=response_schema,
                                    ),
                                    timeout=self.timeout_seconds,
                                )
                            clean_repaired = repaired_text.strip()
                            if clean_repaired.startswith("```json"):
                                clean_repaired = clean_repaired[7:]
                            if clean_repaired.startswith("```"):
                                clean_repaired = clean_repaired[3:]
                            if clean_repaired.endswith("```"):
                                clean_repaired = clean_repaired[:-3]
                            parsed = response_schema.model_validate_json(clean_repaired.strip())
                            if self.use_cache:
                                _LLM_CACHE_STORE[prompt_hash] = parsed
                            return parsed

                    return text

                except Exception as error:
                    if not _is_retryable(error) or attempt >= self.max_retries:
                        raise
                    await self._sleep(min(2**attempt, MAX_BACKOFF_SECONDS))

            raise AssertionError("unreachable")

        except Exception as error:
            logger.warning(
                "LLM call failed",
                extra={
                    "provider": self.provider,
                    "prompt_chars": prompt_length,
                    "prompt_sha256": prompt_hash,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "error_type": type(error).__name__,
                },
            )
            raise
        finally:
            if completed:
                logger.info(
                    "LLM call completed",
                    extra={
                        "provider": self.provider,
                        "prompt_chars": prompt_length,
                        "prompt_sha256": prompt_hash,
                        "input_tokens": usage_info.get("prompt_tokens"),
                        "output_tokens": usage_info.get("candidates_tokens"),
                        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )


class _BoundLLMClient:
    """Adapter exposing LLMClient through an async ``ainvoke`` surface."""

    def __init__(self, client: LLMClient, tools: Sequence[Any]) -> None:
        self._client = client
        self._tools = tuple(tools)

    async def ainvoke(self, messages: Sequence[Any]) -> Any:
        return await self._client.chat(messages, tools=self._tools)


class MockLLMClient:
    """Test double that returns scripted responses without provider access."""

    def __init__(self, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: Sequence[Any],
        tools: Sequence[Any] | None = None,
        response_schema: type[BaseModel] | Mapping[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> Any:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "response_schema": response_schema,
                "temperature": temperature,
            }
        )
        if not self._responses:
            raise AssertionError("MockLLMClient has no scripted responses remaining")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response
