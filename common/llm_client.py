"""Provider-neutral, redacting chat client for shared LLM calls."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
import logging
import os
import time
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ValidationError

from common.redaction import redact


logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_REQUESTS_PER_MINUTE = 10.0
DEFAULT_RETRIES = 2
MAX_BACKOFF_SECONDS = 8.0


class LLMUnavailable(RuntimeError):
    """Raised when LLM calls have been intentionally disabled or cannot be made."""


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, BaseMessage):
        updates: dict[str, Any] = {"content": _redact_value(value.content)}
        if hasattr(value, "tool_calls"):
            updates["tool_calls"] = _redact_value(value.tool_calls)
        if hasattr(value, "additional_kwargs"):
            updates["additional_kwargs"] = _redact_value(value.additional_kwargs)
        return value.model_copy(update=updates)
    if isinstance(value, Mapping):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _serialized_messages(messages: Sequence[Any]) -> str:
    safe: list[Any] = []
    for message in messages:
        if isinstance(message, BaseMessage):
            safe.append(
                {
                    "role": message.type,
                    "content": _redact_value(message.content),
                    "tool_calls": _redact_value(getattr(message, "tool_calls", [])),
                }
            )
        else:
            safe.append(_redact_value(message))
    return json.dumps(safe, default=str, sort_keys=True, ensure_ascii=True)


def _message_hash_and_length(messages: Sequence[Any]) -> tuple[str, int]:
    content = _serialized_messages(messages)
    return sha256(content.encode("utf-8")).hexdigest(), len(content)


def _status_code(error: BaseException) -> int | None:
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _retryable(error: BaseException) -> bool:
    status = _status_code(error)
    return status == 429 or (status is not None and 500 <= status <= 599)


def _token_usage(response: Any) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, Mapping) or not usage:
        usage = getattr(response, "response_metadata", {}).get("token_usage", {})
    if not isinstance(usage, Mapping):
        return None, None
    input_tokens = usage.get("input_tokens", usage.get("prompt_token_count"))
    output_tokens = usage.get("output_tokens", usage.get("candidates_token_count"))
    return (
        input_tokens if isinstance(input_tokens, int) else None,
        output_tokens if isinstance(output_tokens, int) else None,
    )


def _is_json_repair_error(error: BaseException) -> bool:
    if isinstance(error, (json.JSONDecodeError, ValidationError)):
        return True
    return type(error).__name__ in {"OutputParserException", "InvalidResponseError"}


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


class _OfflineBoundModel:
    async def ainvoke(self, *_: Any, **kwargs: Any) -> Any:
        del kwargs
        raise LLMUnavailable("LLM calls are disabled by LLM_OFFLINE=1")


class LLMClient:
    """Redacting chat wrapper with provider selection, retries, and rate limiting."""

    def __init__(
        self,
        model: BaseChatModel | None = None,
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
        self.model: BaseChatModel | None = model

        if self.offline or model is not None:
            return

        if self.provider != "gemini":
            raise ValueError("LLM_PROVIDER must be 'gemini'")
        configured_key = api_key or os.getenv("GEMINI_API_KEY")
        if not configured_key:
            raise ValueError("GEMINI_API_KEY must be configured for the Gemini provider")
        from langchain_google_genai import ChatGoogleGenerativeAI

        self.model = ChatGoogleGenerativeAI(
            model=model_name or os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            google_api_key=configured_key,
            temperature=0,
            max_retries=0,
            timeout=timeout_seconds,
        )

    def bind_tools(self, tools: Sequence[BaseTool]) -> Any:
        """Return the configured model with the supplied read-only tools bound."""
        if self.offline:
            return _OfflineBoundModel()
        if self.model is None:
            raise LLMUnavailable("No LLM model is configured")
        return self.model.bind_tools(list(tools))

    async def _invoke(self, model: Any, messages: Sequence[Any]) -> Any:
        for attempt in range(self.max_retries + 1):
            await self.rate_limiter.acquire()
            try:
                return await asyncio.wait_for(
                    model.ainvoke(messages), timeout=self.timeout_seconds
                )
            except Exception as error:
                if not _retryable(error) or attempt >= self.max_retries:
                    raise
                await self._sleep(min(2**attempt, MAX_BACKOFF_SECONDS))
        raise AssertionError("unreachable")

    async def chat(
        self,
        messages: Sequence[Any],
        tools: Sequence[BaseTool] | None = None,
        response_schema: type[BaseModel] | Mapping[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> Any:
        """Send redacted messages and return a chat response or validated schema value."""
        if self.offline:
            raise LLMUnavailable("LLM calls are disabled by LLM_OFFLINE=1")
        if self.model is None:
            raise LLMUnavailable("No LLM model is configured")
        if not 0.0 <= temperature <= 2.0:
            raise ValueError("temperature must be between 0 and 2")

        safe_messages = _redact_value(messages)
        prompt_hash, prompt_length = _message_hash_and_length(safe_messages)
        started = time.perf_counter()
        response: Any = None
        completed = False
        try:
            request_model: Any = self.model
            if tools:
                request_model = request_model.bind_tools(list(tools))
            request_model = request_model.bind(temperature=temperature)
            if response_schema is None:
                response = await self._invoke(request_model, safe_messages)
                completed = True
                return response

            structured_model = request_model.with_structured_output(
                response_schema, include_raw=True
            )
            repair_messages = list(safe_messages)
            for repair_attempt in range(2):
                try:
                    result = await self._invoke(structured_model, repair_messages)
                    if isinstance(result, Mapping) and "parsed" in result:
                        if result.get("parsing_error") is not None:
                            raise result["parsing_error"]
                        if result.get("parsed") is None:
                            raise ValueError("structured response could not be parsed")
                        response = result.get("raw")
                        completed = True
                        return result.get("parsed")
                    response = result
                    completed = True
                    return result
                except Exception as error:
                    if repair_attempt or not _is_json_repair_error(error):
                        raise
                    repair_messages.append(
                        HumanMessage(
                            content=(
                                "Your previous response did not match the required JSON "
                                "schema. Return a corrected response only, with valid JSON "
                                "that conforms to the supplied schema."
                            )
                        )
                    )
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
                    "status_code": _status_code(error),
                },
            )
            raise
        finally:
            input_tokens, output_tokens = _token_usage(response)
            if completed:
                logger.info(
                    "LLM call completed",
                    extra={
                        "provider": self.provider,
                        "prompt_chars": prompt_length,
                        "prompt_sha256": prompt_hash,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )


class MockLLMClient:
    """Test double that returns scripted responses without provider access."""

    def __init__(self, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: Sequence[Any],
        tools: Sequence[BaseTool] | None = None,
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
