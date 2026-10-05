import json
import logging

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel
import pytest

from common.llm_client import (
    LLMClient,
    LLMUnavailable,
    MockLLMClient,
    TokenBucketRateLimiter,
)


class Summary(BaseModel):
    summary: str


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def bind(self, **kwargs):
        return self

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema, *, include_raw):
        assert schema is Summary
        assert include_raw is True
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class StatusError(Exception):
    def __init__(self, status_code):
        super().__init__("provider error")
        self.status_code = status_code


def make_client(model, **kwargs):
    return LLMClient(
        model=model,
        requests_per_minute=600,
        sleep=_no_sleep,
        **kwargs,
    )


async def _no_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_chat_redacts_messages_and_logs_only_prompt_metadata(caplog):
    model = FakeModel([AIMessage(content="answer")])
    client = make_client(model)
    caplog.set_level(logging.INFO, logger="common.llm_client")

    response = await client.chat([HumanMessage(content="token=supersecret")])

    assert response.content == "answer"
    assert "token=[REDACTED]" in model.calls[0][0].content
    assert "supersecret" not in caplog.text
    assert "token=[REDACTED]" not in caplog.text
    record = next(record for record in caplog.records if record.message == "LLM call completed")
    assert record.prompt_sha256
    assert record.prompt_chars > 0
    assert record.input_tokens is None


@pytest.mark.asyncio
async def test_chat_retries_rate_limit_and_server_errors_with_backoff():
    sleeps = []

    async def record_sleep(seconds):
        sleeps.append(seconds)

    model = FakeModel([StatusError(429), StatusError(503), AIMessage(content="ok")])
    client = LLMClient(
        model=model,
        requests_per_minute=600,
        sleep=record_sleep,
    )

    response = await client.chat([{"role": "user", "content": "hello"}])

    assert response.content == "ok"
    assert len(model.calls) == 3
    assert sleeps == [1, 2]


@pytest.mark.asyncio
async def test_chat_does_not_retry_non_transient_errors():
    model = FakeModel([StatusError(400), AIMessage(content="unused")])
    client = make_client(model)

    with pytest.raises(StatusError):
        await client.chat([{"role": "user", "content": "hello"}])

    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_structured_output_gets_one_automatic_repair_attempt():
    parse_error = json.JSONDecodeError("invalid JSON", "{", 1)
    result = Summary(summary="repaired")
    model = FakeModel(
        [
            {"raw": AIMessage(content="{"), "parsed": None, "parsing_error": parse_error},
            {"raw": AIMessage(content='{"summary":"repaired"}'), "parsed": result, "parsing_error": None},
        ]
    )
    client = make_client(model)

    response = await client.chat(
        [HumanMessage(content="summarize")],
        response_schema=Summary,
    )

    assert response == result
    assert len(model.calls) == 2
    assert "corrected response only" in model.calls[1][-1].content


@pytest.mark.asyncio
async def test_offline_mode_raises_for_chat_and_bound_tools(monkeypatch):
    monkeypatch.setenv("LLM_OFFLINE", "1")
    client = LLMClient()

    with pytest.raises(LLMUnavailable, match="LLM_OFFLINE"):
        await client.chat([{"role": "user", "content": "hello"}])
    with pytest.raises(LLMUnavailable, match="LLM_OFFLINE"):
        await client.bind_tools([]).ainvoke([])


@pytest.mark.asyncio
async def test_token_bucket_waits_for_refill():
    current_time = [0.0]
    sleeps = []

    async def advance_time(seconds):
        sleeps.append(seconds)
        current_time[0] += seconds

    limiter = TokenBucketRateLimiter(
        1,
        sleep=advance_time,
        clock=lambda: current_time[0],
    )

    await limiter.acquire()
    await limiter.acquire()

    assert sleeps == [60.0]


@pytest.mark.asyncio
async def test_mock_client_returns_scripted_responses_and_records_calls():
    mock = MockLLMClient(["first", RuntimeError("scripted failure")])
    first = await mock.chat([{"role": "user", "content": "one"}], temperature=0.4)

    assert first == "first"
    assert mock.calls[0]["temperature"] == 0.4
    with pytest.raises(RuntimeError, match="scripted failure"):
        await mock.chat([{"role": "user", "content": "two"}])
    with pytest.raises(AssertionError, match="no scripted responses"):
        await mock.chat([])
