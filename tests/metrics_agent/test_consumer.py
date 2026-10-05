import asyncio
import json
import logging
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agents.metrics_agent.consumer import MetricsAgent, connect_with_retry, parse_event


class FakeConnect:
    """Fails `failures` times with ConnectionRefusedError, then returns a sentinel."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionRefusedError("rabbitmq not ready")
        return "connection"


def run_retry(connect: FakeConnect, retries: int) -> tuple[str, list[float]]:
    delays: list[float] = []

    async def no_sleep(delay: float) -> None:
        delays.append(delay)

    result = asyncio.run(connect_with_retry(connect, retries=retries, backoff_seconds=1.0, sleep=no_sleep))
    return result, delays


def test_connect_retries_then_succeeds_with_exponential_backoff() -> None:
    connect = FakeConnect(failures=2)
    result, delays = run_retry(connect, retries=5)
    assert result == "connection"
    assert connect.calls == 3
    assert delays == [1.0, 2.0]


def test_connect_raises_after_bounded_retries_exhausted() -> None:
    connect = FakeConnect(failures=100)
    with pytest.raises(ConnectionRefusedError):
        run_retry(connect, retries=3)
    assert connect.calls == 4


def test_connect_does_not_retry_non_connection_errors() -> None:
    async def broken() -> str:
        raise ValueError("bad url")

    with pytest.raises(ValueError):
        asyncio.run(connect_with_retry(broken, retries=3, backoff_seconds=1.0))


def test_parse_event_accepts_valid_payload_and_ignores_extras() -> None:
    incident_id = uuid4()
    body = json.dumps({"incident_id": str(incident_id), "job_name": "orders", "unexpected": 1}).encode()
    event = parse_event(body)
    assert event.incident_id == incident_id
    assert event.job_name == "orders"


def test_parse_event_rejects_missing_incident_id() -> None:
    with pytest.raises(ValidationError):
        parse_event(b'{"job_name": "orders"}')


def test_parse_event_rejects_invalid_json() -> None:
    with pytest.raises(ValueError):
        parse_event(b"not json")


def test_agent_logs_ready_without_querying_prometheus(caplog: pytest.LogCaptureFixture) -> None:
    tool = MagicMock()
    agent = MetricsAgent(tool)
    event = parse_event(json.dumps({"incident_id": str(uuid4())}).encode())
    with caplog.at_level(logging.INFO):
        agent.handle_incident(event)
    assert "ready for investigation" in caplog.text
    tool.query.assert_not_called()
