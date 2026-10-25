"""Comprehensive unit tests for Jenkins validation webhook, incident resolution, and reinvestigation."""

from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.db.audit_models import AuditEvent, AuditEventType, ProposalStatus, RemediationProposal
from backend.db.database import get_db
from backend.db.models import Incident
from backend.main import app
from backend.routes.validation import version_incident_evidence


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {
        "X-Webhook-Token": settings.WEBHOOK_SHARED_SECRET or "a8f9c2d1e3f4b5a6c7d8e9f0a1b2c3d4",
        "Content-Type": "application/json",
    }


def _make_incident(incident_id: str, attempt: int = 0, status: str = "OPEN") -> Incident:
    return Incident(
        id=incident_id,
        job_name="target-app/main",
        build_number=10,
        build_url="http://jenkins:8080/job/target-app/10/",
        branch="main",
        git_commit="commit-1234",
        failed_stage="Test",
        status=status,
        event_key=f"event-{incident_id}",
        remediation_attempt=attempt,
        created_at=datetime.now(timezone.utc),
    )


def _make_proposal(incident_id: str, attempt: int = 0, pr_url: str = "https://github.com/org/repo/pull/42") -> RemediationProposal:
    return RemediationProposal(
        id=42,
        incident_id=incident_id,
        attempt=attempt,
        status=ProposalStatus.DRAFT.value,
        pull_request_url=pr_url,
        rationale="Fix broken order verification logic",
        created_at=datetime.now(timezone.utc),
    )


def test_validation_missing_auth(client):
    resp = client.post("/webhooks/jenkins/validation", json={"job_name": "target-app", "result": "SUCCESS"})
    assert resp.status_code == 401


def test_validation_orphan_when_no_incident_found(client, auth_headers):
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db

    resp = client.post(
        "/webhooks/jenkins/validation",
        json={"job_name": "target-app/main", "build_number": 12, "result": "SUCCESS"},
        headers=auth_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "validation_received"
    assert "orphan validation" in data.get("message", "")

    app.dependency_overrides.clear()


def test_validation_resolves_explicit_incident_id_success(client, auth_headers):
    inc_id = str(uuid4())
    incident = _make_incident(inc_id, attempt=0)

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    # Query 1: Incident lookup -> returns incident
    # Query 2: AuditEvent idempotency -> returns empty
    # Query 3: Proposal lookup -> returns proposal
    mock_inc_res = MagicMock()
    mock_inc_res.scalar_one_or_none.return_value = incident

    mock_audit_res = MagicMock()
    mock_audit_res.scalars.return_value.all.return_value = []

    proposal = _make_proposal(inc_id, attempt=0)
    mock_prop_res = MagicMock()
    mock_prop_res.scalar_one_or_none.return_value = proposal

    mock_db.execute.side_effect = [mock_inc_res, mock_audit_res, mock_prop_res]

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db

    with patch("backend.routes.validation.publisher.publish_validation_completed", new_callable=AsyncMock) as mock_val_pub:
        resp = client.post(
            "/webhooks/jenkins/validation",
            json={
                "job_name": "target-app/main",
                "build_number": 15,
                "result": "SUCCESS",
                "incident_id": inc_id,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202
        assert incident.status == "RESOLVED"
        assert proposal.status == ProposalStatus.VALIDATION_PASSED.value
        assert mock_val_pub.called
        call_payload = mock_val_pub.call_args[0][0]
        assert call_payload["incident_id"] == inc_id
        assert call_payload["status"] == "SUCCESS"

    app.dependency_overrides.clear()


def test_validation_resolves_branch_name(client, auth_headers):
    inc_id = str(uuid4())
    incident = _make_incident(inc_id, attempt=1)

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    mock_inc_res = MagicMock()
    mock_inc_res.scalar_one_or_none.return_value = incident

    mock_audit_res = MagicMock()
    mock_audit_res.scalars.return_value.all.return_value = []

    mock_prop_res = MagicMock()
    mock_prop_res.scalar_one_or_none.return_value = None

    mock_db.execute.side_effect = [mock_inc_res, mock_audit_res, mock_prop_res]

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db

    with patch("backend.routes.validation.publisher.publish_validation_completed", new_callable=AsyncMock) as mock_val_pub:
        resp = client.post(
            "/webhooks/jenkins/validation",
            json={
                "job_name": "target-app/main",
                "build_number": 16,
                "result": "SUCCESS",
                "branch": f"ai-fix/{inc_id}-1",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202
        assert resp.json()["incident_id"] == inc_id
        assert incident.status == "RESOLVED"

    app.dependency_overrides.clear()


def test_validation_resolves_change_id_from_remediation_table(client, auth_headers):
    inc_id = str(uuid4())
    incident = _make_incident(inc_id, attempt=0)
    proposal = _make_proposal(inc_id, attempt=0, pr_url="https://github.com/org/repo/pull/99")

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    # Query 1: PR proposal lookup -> returns proposal
    mock_pr_res = MagicMock()
    mock_pr_res.scalar_one_or_none.return_value = proposal

    # Query 2: Incident lookup -> returns incident
    mock_inc_res = MagicMock()
    mock_inc_res.scalar_one_or_none.return_value = incident

    # Query 3: AuditEvent idempotency -> returns empty
    mock_audit_res = MagicMock()
    mock_audit_res.scalars.return_value.all.return_value = []

    # Query 4: Proposal update lookup -> returns proposal
    mock_prop_update_res = MagicMock()
    mock_prop_update_res.scalar_one_or_none.return_value = proposal

    mock_db.execute.side_effect = [mock_pr_res, mock_inc_res, mock_audit_res, mock_prop_update_res]

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db

    with patch("backend.routes.validation.publisher.publish_validation_completed", new_callable=AsyncMock) as mock_val_pub:
        resp = client.post(
            "/webhooks/jenkins/validation",
            json={
                "job_name": "target-app/main",
                "build_number": 17,
                "result": "SUCCESS",
                "change_id": "99",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202
        assert resp.json()["incident_id"] == inc_id
        assert incident.status == "RESOLVED"

    app.dependency_overrides.clear()


def test_validation_failure_triggers_reinvestigation_under_max_attempts(client, auth_headers, monkeypatch):
    monkeypatch.setenv("MAX_ATTEMPTS", "2")
    inc_id = str(uuid4())
    # attempt 0 < 2 -> should reinvestigate with attempt 1
    incident = _make_incident(inc_id, attempt=0)

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    mock_inc_res = MagicMock()
    mock_inc_res.scalar_one_or_none.return_value = incident

    mock_audit_res = MagicMock()
    mock_audit_res.scalars.return_value.all.return_value = []

    proposal = _make_proposal(inc_id, attempt=0)
    mock_prop_res = MagicMock()
    mock_prop_res.scalar_one_or_none.return_value = proposal

    # Proposal lookup for status update, then proposal lookup for prev summary
    mock_db.execute.side_effect = [mock_inc_res, mock_audit_res, mock_prop_res, mock_prop_res]

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db

    with patch("backend.routes.validation.publisher.publish_validation_completed", new_callable=AsyncMock) as mock_val_pub, \
         patch("backend.routes.validation.publisher.publish_incident_reinvestigate", new_callable=AsyncMock) as mock_reinv_pub:

        resp = client.post(
            "/webhooks/jenkins/validation",
            json={
                "job_name": "target-app/main",
                "build_number": 18,
                "result": "FAILURE",
                "incident_id": inc_id,
                "remediation_attempt": 0,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202
        assert incident.remediation_attempt == 1
        assert proposal.status == ProposalStatus.VALIDATION_FAILED.value

        assert mock_val_pub.called
        assert mock_reinv_pub.called
        reinv_payload = mock_reinv_pub.call_args[0][0]
        assert reinv_payload["incident_id"] == inc_id
        assert reinv_payload["attempt"] == 1
        assert "Validation failed: FAILURE" in reinv_payload["previous_failure_summary"]

    app.dependency_overrides.clear()


def test_validation_failure_escalates_when_max_attempts_reached(client, auth_headers, monkeypatch):
    monkeypatch.setenv("MAX_ATTEMPTS", "2")
    inc_id = str(uuid4())
    # attempt 2 >= 2 -> should escalate
    incident = _make_incident(inc_id, attempt=2)

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    mock_inc_res = MagicMock()
    mock_inc_res.scalar_one_or_none.return_value = incident

    mock_audit_res = MagicMock()
    mock_audit_res.scalars.return_value.all.return_value = []

    proposal = _make_proposal(inc_id, attempt=2)
    mock_prop_res = MagicMock()
    mock_prop_res.scalar_one_or_none.return_value = proposal

    mock_db.execute.side_effect = [mock_inc_res, mock_audit_res, mock_prop_res]

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db

    with patch("backend.routes.validation.publisher.publish_validation_completed", new_callable=AsyncMock) as mock_val_pub, \
         patch("backend.routes.validation.publisher.publish_incident_reinvestigate", new_callable=AsyncMock) as mock_reinv_pub:

        resp = client.post(
            "/webhooks/jenkins/validation",
            json={
                "job_name": "target-app/main",
                "build_number": 19,
                "result": "FAILURE",
                "incident_id": inc_id,
                "remediation_attempt": 2,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202
        assert incident.status == "ESCALATED"
        assert mock_val_pub.called
        # Never loop forever: should not reinvestigate
        assert not mock_reinv_pub.called

    app.dependency_overrides.clear()


def test_validation_idempotency_duplicate_handling(client, auth_headers):
    inc_id = str(uuid4())
    incident = _make_incident(inc_id, attempt=0)

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    mock_inc_res = MagicMock()
    mock_inc_res.scalar_one_or_none.return_value = incident

    # Existing audit event shows this build was already processed
    existing_audit_event = AuditEvent(
        id=1,
        incident_id=inc_id,
        actor="jenkins",
        event_type=AuditEventType.REVALIDATION_COMPLETED.value,
        payload={"job_name": "target-app/main", "build_number": 20, "result": "SUCCESS"},
    )
    mock_audit_res = MagicMock()
    mock_audit_res.scalars.return_value.all.return_value = [existing_audit_event]

    mock_db.execute.side_effect = [mock_inc_res, mock_audit_res]

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db

    with patch("backend.routes.validation.publisher.publish_validation_completed", new_callable=AsyncMock) as mock_val_pub:
        resp = client.post(
            "/webhooks/jenkins/validation",
            json={
                "job_name": "target-app/main",
                "build_number": 20,
                "result": "SUCCESS",
                "incident_id": inc_id,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "already_processed"
        assert not mock_val_pub.called

    app.dependency_overrides.clear()


def test_version_incident_evidence_archives_files(tmp_path, monkeypatch):
    inc_id = str(uuid4())
    evidence_dir = tmp_path / "data" / "evidence" / inc_id
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "jenkins_agent.json").write_text('{"version": 1, "data": "attempt1"}', encoding="utf-8")

    bb_dir = tmp_path / "data" / "blackboard" / inc_id
    bb_dir.mkdir(parents=True)
    (bb_dir / "history.jsonl").write_text('{"entry": 1}\n', encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    version_incident_evidence(inc_id, attempt=1)

    assert (evidence_dir / "jenkins_agent.attempt_1.json").exists()
    assert (bb_dir / "history_attempt_1.jsonl").exists()
