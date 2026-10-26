"""The Code agent as an A2A task server: auth, pointer-only reply, idempotency, per-task client."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agents.code_agent.a2a_app import agent_card, create_app
from common.blackboard import BlackboardEntry, FileBlackboard
from common.models import Evidence, FailureTaxonomy

HEADERS = {"Authorization": "Bearer test-token"}


class FakeGithub:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeAgent:
    def __init__(self, fail: bool = False) -> None:
        self.events = []
        self.fail = fail

    async def run_event(self, event):
        self.events.append(event)
        if self.fail:
            raise RuntimeError("github exploded")
        return Evidence(
            incident_id=event.incident_id,
            agent="code_agent",
            created_at=datetime.now(timezone.utc),
            status="completed",
            failure_type=FailureTaxonomy.CONFIG_ERROR,
            summary="a config key was changed",
            confidence=0.6,
            redaction_applied=True,
        )


class Setup:
    def __init__(self, tmp_path, monkeypatch, fail: bool = False) -> None:
        self.blackboard = FileBlackboard(tmp_path / "blackboard")
        monkeypatch.setattr("common.a2a_server.get_blackboard", lambda: self.blackboard)
        monkeypatch.setattr("agents.code_agent.a2a_app.get_blackboard", lambda: self.blackboard)
        monkeypatch.setenv("A2A_SHARED_TOKEN", "test-token")
        self.agents: list[FakeAgent] = []
        self.clients: list[FakeGithub] = []
        self.fail = fail

        def builder():
            agent, github = FakeAgent(self.fail), FakeGithub()
            self.agents.append(agent)
            self.clients.append(github)
            return agent, github

        self.client = TestClient(create_app(builder, url="http://code-agent:9103"))

    def post_context(self, incident: UUID) -> None:
        payload = {
            "job_name": "target-app/main",
            "build_number": 14,
            "build_url": "http://jenkins/job/x/14/",
            "branch": "main",
            "git_commit": "a" * 40,
            "timestamp": "2026-10-26T01:57:35+00:00",
        }
        self.blackboard.post(
            BlackboardEntry(incident_id=incident, section="context", key="incident_event", author="backend", payload=payload)
        )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return Setup(tmp_path, monkeypatch)


def test_card_names_the_agent_and_the_skill():
    card = agent_card("http://code-agent:9103")
    assert card["name"] == "code_agent" and [s["name"] for s in card["skills"]] == ["investigate_incident"]


def test_health_and_card_are_served(setup):
    assert setup.client.get("/health").json() == {"status": "ok"}
    assert setup.client.get("/.well-known/agent-card.json").json()["name"] == "code_agent"


def test_requires_the_shared_token(setup):
    assert setup.client.post("/tasks", json={"incident_id": str(uuid4())}).status_code == 401


def test_task_runs_the_investigation_and_returns_a_pointer_only(setup):
    incident = uuid4()
    setup.post_context(incident)

    response = setup.client.post("/tasks", json={"incident_id": str(incident), "attempt": 0}, headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed" and "summary" not in body
    assert body["blackboard_ref"] == {"incident_id": str(incident), "section": "evidence", "key": "code_agent", "version": 1}
    assert setup.agents[0].events[0].build_number == 14
    stored = setup.blackboard.read(incident, section="evidence", key="code_agent")
    assert stored is not None and stored.payload["failure_type"] == "config_error"


def test_the_github_client_is_closed_after_each_task(setup):
    incident = uuid4()
    setup.post_context(incident)
    setup.client.post("/tasks", json={"incident_id": str(incident)}, headers=HEADERS)
    assert [c.closed for c in setup.clients] == [True]


def test_each_task_gets_its_own_agent_and_client(setup):
    incident = uuid4()
    setup.post_context(incident)
    setup.client.post("/tasks", json={"incident_id": str(incident), "attempt": 0}, headers=HEADERS)
    setup.client.post("/tasks", json={"incident_id": str(incident), "attempt": 1}, headers=HEADERS)
    assert len(setup.agents) == 2 and all(c.closed for c in setup.clients)


def test_repeating_the_same_incident_and_attempt_investigates_once(setup):
    incident = uuid4()
    setup.post_context(incident)
    first = setup.client.post("/tasks", json={"incident_id": str(incident)}, headers=HEADERS)
    second = setup.client.post("/tasks", json={"incident_id": str(incident)}, headers=HEADERS)
    assert first.json() == second.json() and len(setup.agents) == 1


def test_missing_incident_context_is_a_failed_task(setup):
    response = setup.client.post("/tasks", json={"incident_id": str(uuid4())}, headers=HEADERS)
    assert response.status_code == 500 and response.json()["status"] == "failed"
    assert setup.agents == []


def test_an_investigation_error_is_reported_as_failed_and_still_closes_the_client(tmp_path, monkeypatch):
    setup = Setup(tmp_path, monkeypatch, fail=True)
    incident = uuid4()
    setup.post_context(incident)
    response = setup.client.post("/tasks", json={"incident_id": str(incident)}, headers=HEADERS)
    assert response.status_code == 500
    assert response.json() == {"status": "failed", "reason": "RuntimeError", "incident_id": str(incident)}
    assert [c.closed for c in setup.clients] == [True]
