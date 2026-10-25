"""Tests for authenticated A2A task execution and Blackboard references."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from common.a2a_server import create_a2a_app
from common.blackboard import BlackboardEntry, FileBlackboard
from common.models import Evidence, FailureTaxonomy


@pytest.fixture
def a2a_setup(tmp_path, monkeypatch):
    blackboard = FileBlackboard(tmp_path / "blackboard")
    monkeypatch.setattr("common.a2a_server.get_blackboard", lambda: blackboard)

    def run_fn(incident_id: str | UUID, attempt: int) -> Evidence:
        evidence = Evidence(
            incident_id=UUID(str(incident_id)),
            agent="jenkins_agent",
            created_at=datetime.now(timezone.utc),
            status="completed",
            failure_type=FailureTaxonomy.UNKNOWN,
            summary=f"Investigation attempt {attempt}",
            confidence=0.2,
            redaction_applied=True,
        )
        blackboard.post(
            BlackboardEntry(
                incident_id=evidence.incident_id,
                section="evidence",
                key="jenkins_agent",
                author="jenkins_agent",
                payload=evidence.model_dump(mode="json"),
            )
        )
        return evidence

    app = create_a2a_app(
        agent_card_data={"name": "jenkins_agent", "skills": ["investigate_incident"]},
        run_fn=run_fn,
    )
    return TestClient(app), blackboard


def test_agent_card_endpoints(a2a_setup):
    client, _ = a2a_setup
    assert client.get("/.well-known/agent-card.json").status_code == 200
    assert client.get("/.well-known/agent.json").status_code == 200


def test_a2a_requires_a_configured_token(a2a_setup, monkeypatch):
    client, _ = a2a_setup
    monkeypatch.delenv("A2A_SHARED_TOKEN", raising=False)

    response = client.post("/tasks", json={"incident_id": str(uuid4())})
    assert response.status_code == 503


def test_a2a_auth_rejection(a2a_setup, monkeypatch):
    client, _ = a2a_setup
    monkeypatch.setenv("A2A_SHARED_TOKEN", "expected-token")
    payload = {"incident_id": str(uuid4())}

    assert client.post("/tasks", json=payload).status_code == 401
    assert client.post(
        "/tasks",
        json=payload,
        headers={"Authorization": "Bearer wrong-token"},
    ).status_code == 401


def test_task_writes_evidence_and_returns_pointer_only(a2a_setup, monkeypatch):
    client, blackboard = a2a_setup
    monkeypatch.setenv("A2A_SHARED_TOKEN", "test-token")
    incident_id = uuid4()
    payload = {"incident_id": str(incident_id), "attempt": 1}
    headers = {"Authorization": "Bearer test-token"}

    response = client.post("/tasks", json=payload, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["incident_id"] == str(incident_id)
    assert "evidence" not in data
    assert data["blackboard_ref"] == {
        "incident_id": str(incident_id),
        "section": "evidence",
        "key": "jenkins_agent",
        "version": 1,
    }
    assert blackboard.read(incident_id, section="evidence", key="jenkins_agent") is not None

    repeated = client.post("/tasks", json=payload, headers=headers)
    assert repeated.status_code == 200
    assert repeated.json() == data


def test_task_rejects_negative_attempt(a2a_setup, monkeypatch):
    client, _ = a2a_setup
    monkeypatch.setenv("A2A_SHARED_TOKEN", "test-token")
    response = client.post(
        "/tasks",
        json={"incident_id": str(uuid4()), "attempt": -1},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 422
