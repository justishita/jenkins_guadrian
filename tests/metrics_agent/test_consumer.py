import json
import logging
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agents.metrics_agent.consumer import MetricsAgent, parse_event


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
