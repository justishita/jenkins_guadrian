"""The Metrics agent as an A2A task server: auth, pointer-only reply, idempotency, errors."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agents.metrics_agent.a2a_app import agent_card, create_app
from common.blackboard import BlackboardEntry, FileBlackboard
from common.models import Evidence, FailureTaxonomy

HEADERS = {"Authorization": "Bearer test-token"}


class FakeAgent:
    """Stands in for MetricsAgent: records the event and returns canned evidence."""

    def __init__(self, fail: bool = False) -> None:
        self.events = []
        self.fail = fail

    async def run_event(self, event):
        self.events.append(event)
        if self.fail:
            raise RuntimeError("prometheus exploded")
        return Evidence(
            incident_id=event.incident_id,
            agent="metrics_agent",
            created_at=datetime.now(timezone.utc),
            status="completed",
            failure_type=FailureTaxonomy.TIMEOUT,
            summary="latency spike",
            confidence=0.7,
            redaction_applied=True,
        )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    blackboard = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr("common.a2a_server.get_blackboard", lambda: blackboard)
    monkeypatch.setattr("agents.metrics_agent.a2a_app.get_blackboard", lambda: blackboard)
    monkeypatch.setenv("A2A_SHARED_TOKEN", "test-token")
    agent = FakeAgent()
    return TestClient(create_app(agent, url="http://metrics-agent:9102")), blackboard, agent


def post_context(blackboard: FileBlackboard, incident: UUID, **overrides) -> None:
    payload = {
        "job_name": "target-app/main",
        "build_number": 14,
        "build_url": "http://jenkins/job/x/14/",
        "branch": "main",
        "git_commit": "0" * 40,
        "timestamp": "2026-10-26T01:57:35+00:00",
        **overrides,
    }
    blackboard.post(
        BlackboardEntry(incident_id=incident, section="context", key="incident_event", author="backend", payload=payload)
    )


def test_card_names_the_agent_and_the_skill():
    card = agent_card("http://metrics-agent:9102")
    assert card["name"] == "metrics_agent" and card["url"] == "http://metrics-agent:9102"
    assert [s["name"] for s in card["skills"]] == ["investigate_incident"]


def test_health_and_card_are_served(setup):
    client, _, _ = setup
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/.well-known/agent-card.json").json()["name"] == "metrics_agent"


def test_requires_the_shared_token(setup):
    client, _, _ = setup
    assert client.post("/tasks", json={"incident_id": str(uuid4())}).status_code == 401
    assert client.post("/tasks", json={"incident_id": str(uuid4())}, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_task_runs_the_investigation_and_returns_a_pointer_only(setup):
    client, blackboard, agent = setup
    incident = uuid4()
    post_context(blackboard, incident)

    response = client.post("/tasks", json={"incident_id": str(incident), "attempt": 0}, headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed" and "summary" not in body and "evidence" not in body
    assert body["blackboard_ref"] == {
        "incident_id": str(incident), "section": "evidence", "key": "metrics_agent", "version": 1,
    }
    assert agent.events[0].incident_id == incident and agent.events[0].build_number == 14
    stored = blackboard.read(incident, section="evidence", key="metrics_agent")
    assert stored is not None and stored.payload["failure_type"] == "timeout"


def test_repeating_the_same_incident_and_attempt_investigates_once(setup):
    client, blackboard, agent = setup
    incident = uuid4()
    post_context(blackboard, incident)
    first = client.post("/tasks", json={"incident_id": str(incident)}, headers=HEADERS)
    second = client.post("/tasks", json={"incident_id": str(incident)}, headers=HEADERS)
    assert first.json() == second.json() and len(agent.events) == 1


def test_a_new_attempt_is_a_new_investigation(setup):
    client, blackboard, agent = setup
    incident = uuid4()
    post_context(blackboard, incident)
    client.post("/tasks", json={"incident_id": str(incident), "attempt": 0}, headers=HEADERS)
    second = client.post("/tasks", json={"incident_id": str(incident), "attempt": 1}, headers=HEADERS)
    assert second.status_code == 200 and len(agent.events) == 2
    assert second.json()["blackboard_ref"]["version"] == 2  # evidence is versioned, not overwritten


def test_missing_incident_context_is_a_failed_task_not_a_crash(setup):
    client, _, agent = setup
    response = client.post("/tasks", json={"incident_id": str(uuid4())}, headers=HEADERS)
    assert response.status_code == 500 and response.json()["status"] == "failed"
    assert agent.events == []


def test_an_investigation_error_is_reported_as_failed(setup):
    client, blackboard, agent = setup
    agent.fail = True
    incident = uuid4()
    post_context(blackboard, incident)
    response = client.post("/tasks", json={"incident_id": str(incident)}, headers=HEADERS)
    assert response.status_code == 500
    assert response.json() == {"status": "failed", "reason": "RuntimeError", "incident_id": str(incident)}


def test_a_negative_attempt_is_rejected(setup):
    client, _, _ = setup
    assert client.post("/tasks", json={"incident_id": str(uuid4()), "attempt": -1}, headers=HEADERS).status_code == 422
