"""Tests for agents/jenkins_agent/a2a_app.py."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from agents.jenkins_agent.a2a_app import app
import agents.jenkins_agent.agent as agent_module
from agents.jenkins_agent.agent import JenkinsInvestigationAgent
from common.blackboard import BlackboardEntry, FileBlackboard
from common.models import Evidence, FailureTaxonomy


@pytest.fixture
def a2a_test_client(tmp_path, monkeypatch):
    monkeypatch.setenv("A2A_SHARED_TOKEN", "secret-a2a-token")
    bb = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr(agent_module, "get_blackboard", lambda: bb)
    monkeypatch.setattr("common.a2a_server.get_blackboard", lambda: bb)

    client = TestClient(app)
    return client, bb


def test_a2a_app_health(a2a_test_client):
    client, _ = a2a_test_client
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_a2a_app_agent_card(a2a_test_client):
    client, _ = a2a_test_client
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "jenkins_agent"
    assert "skills" in data


def test_a2a_app_tasks_requires_auth(a2a_test_client):
    client, _ = a2a_test_client
    resp = client.post("/tasks", json={"incident_id": str(uuid4())})
    assert resp.status_code == 401


def test_a2a_app_tasks_executes(a2a_test_client, monkeypatch):
    client, bb = a2a_test_client
    incident_id = uuid4()

    # Seed incident event context
    bb.post(
        BlackboardEntry(
            incident_id=incident_id,
            section="context",
            key="incident_event",
            author="backend",
            payload={
                "job_name": "target-app/main",
                "build_number": 10,
                "build_url": "http://jenkins:8080/job/target-app/job/main/10/",
                "branch": "main",
                "git_commit": "abc1234",
                "failed_stage": "Test",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    )

    ev = Evidence(
        incident_id=incident_id,
        agent="jenkins_agent",
        created_at=datetime.now(timezone.utc),
        status="completed",
        failure_type=FailureTaxonomy.UNKNOWN,
        summary="A2A app run test",
        confidence=0.5,
        redaction_applied=True,
    )

    async def mock_run_event(self, event):
        bb.post(
            BlackboardEntry(
                incident_id=event.incident_id,
                section="evidence",
                key="jenkins_agent",
                author="jenkins_agent",
                payload=ev.model_dump(mode="json"),
            )
        )
        return ev

    monkeypatch.setattr(JenkinsInvestigationAgent, "run_event", mock_run_event)

    headers = {"Authorization": "Bearer secret-a2a-token"}
    resp = client.post(
        "/tasks",
        headers=headers,
        json={"incident_id": str(incident_id), "attempt": 0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["incident_id"] == str(incident_id)
