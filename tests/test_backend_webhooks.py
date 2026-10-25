from datetime import datetime, timezone
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.config import settings
from backend.db.database import get_db
from backend.db.models import Incident
from backend.models.events import JenkinsFailureEvent


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {
        "X-Webhook-Token": settings.WEBHOOK_SHARED_SECRET or "a8f9c2d1e3f4b5a6c7d8e9f0a1b2c3d4",
        "Content-Type": "application/json",
    }


@pytest.fixture
def valid_payload():
    return {
        "job_name": "target-app/main",
        "build_number": 10,
        "build_url": "http://jenkins:8080/job/target-app/job/main/10/",
        "branch": "main",
        "git_commit": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
        "failed_stage": "Test",
        "timestamp": "2026-10-01T09:30:00Z",
        "incident_id": None,
        "remediation_attempt": 0,
    }


def test_health_healthy(client):
    with patch("backend.routes.health.check_db_health", new_callable=AsyncMock) as mock_db, \
         patch("backend.routes.health.publisher.is_healthy", new_callable=AsyncMock) as mock_rmq:
        mock_db.return_value = True
        mock_rmq.return_value = True

        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["postgres"] == "healthy"
        assert data["rabbitmq"] == "healthy"


def test_health_degraded(client):
    with patch("backend.routes.health.check_db_health", new_callable=AsyncMock) as mock_db, \
         patch("backend.routes.health.publisher.is_healthy", new_callable=AsyncMock) as mock_rmq:
        mock_db.return_value = False
        mock_rmq.return_value = True

        resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["postgres"] == "unhealthy"


def test_webhook_missing_auth(client, valid_payload):
    resp = client.post("/webhooks/jenkins", json=valid_payload)
    assert resp.status_code == 401
    assert "Missing or unauthorized" in resp.json()["detail"]


def test_webhook_invalid_auth(client, valid_payload):
    resp = client.post(
        "/webhooks/jenkins",
        json=valid_payload,
        headers={"X-Webhook-Token": "wrong-secret-token"},
    )
    assert resp.status_code == 401
    assert "Invalid webhook token" in resp.json()["detail"]


def test_webhook_successful_incident_creation(client, auth_headers, valid_payload):
    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    # No existing incident
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db

    with patch("backend.incidents.publisher.publish_incident_created", new_callable=AsyncMock) as mock_pub:
        mock_pub.return_value = True

        resp = client.post("/webhooks/jenkins", json=valid_payload, headers=auth_headers)
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert "incident_id" in data

        # Verify DB add and commit was called
        assert mock_db.add.called
        assert mock_db.commit.called
        # Verify RabbitMQ publish was invoked
        assert mock_pub.called
        call_payload = mock_pub.call_args[0][0]
        assert call_payload["job_name"] == valid_payload["job_name"]
        assert call_payload["build_number"] == valid_payload["build_number"]

    app.dependency_overrides.clear()


def test_webhook_idempotency_retries_event_delivery(client, auth_headers, valid_payload):
    # Simulate existing incident in DB
    existing_incident = Incident(
        id="11111111-2222-3333-4444-555555555555",
        job_name=valid_payload["job_name"],
        build_number=valid_payload["build_number"],
        build_url=valid_payload["build_url"],
        branch=valid_payload["branch"],
        git_commit=valid_payload["git_commit"],
        failed_stage=valid_payload["failed_stage"],
        status="OPEN",
        event_key="some-key",
        remediation_attempt=0,
        created_at=datetime.now(timezone.utc),
    )

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_incident
    mock_db.execute.return_value = mock_result

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db

    with patch("backend.incidents.publisher.publish_incident_created", new_callable=AsyncMock) as mock_pub:
        mock_pub.return_value = True
        resp = client.post("/webhooks/jenkins", json=valid_payload, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "already_processed"
        assert data["incident_id"] == "11111111-2222-3333-4444-555555555555"

        # Re-delivery allows a retry to recover from an earlier publish failure.
        assert mock_pub.called

    app.dependency_overrides.clear()


def test_webhook_body_size_limit(client, auth_headers, valid_payload):
    headers = dict(auth_headers)
    headers["content-length"] = "70000"  # Exceeds 64KB limit

    resp = client.post("/webhooks/jenkins", json=valid_payload, headers=headers)
    assert resp.status_code == 413
    assert "exceeds maximum size" in resp.json()["detail"]


def test_webhook_validation_endpoint(client, auth_headers):
    resp = client.post(
        "/webhooks/jenkins/validation",
        json={"job_name": "target-app/main", "result": "SUCCESS"},
        headers=auth_headers,
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "validation_received"


def test_webhook_posts_to_blackboard_and_is_idempotent(client, auth_headers, valid_payload, tmp_path):
    from common.blackboard import FileBlackboard, get_blackboard
    import common.blackboard as bb_module
    from uuid import UUID

    test_bb = FileBlackboard(root=tmp_path / "blackboard")
    bb_module._default_blackboard = test_bb

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    # First call: no existing incident
    mock_result_none = MagicMock()
    mock_result_none.scalar_one_or_none.return_value = None

    # Incident created
    new_incident = Incident(
        id="11111111-2222-3333-4444-555555555555",
        job_name=valid_payload["job_name"],
        build_number=valid_payload["build_number"],
        build_url=valid_payload["build_url"],
        branch=valid_payload["branch"],
        git_commit=valid_payload["git_commit"],
        failed_stage=valid_payload["failed_stage"],
        status="OPEN",
        event_key="unique-key",
        remediation_attempt=0,
    )
    mock_db.execute.return_value = mock_result_none

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db

    payload = dict(valid_payload)
    payload["incident_id"] = "11111111-2222-3333-4444-555555555555"

    with patch("backend.incidents.publisher.publish_incident_created", new_callable=AsyncMock) as mock_pub:
        mock_pub.return_value = True
        resp = client.post("/webhooks/jenkins", json=payload, headers=auth_headers)
        assert resp.status_code == 202

        # Assert Blackboard entry was posted
        entry = test_bb.read(UUID("11111111-2222-3333-4444-555555555555"), section="context", key="incident_event")
        assert entry is not None
        assert entry.version == 1
        assert entry.payload["job_name"] == valid_payload["job_name"]

        # Duplicate call: existing incident is returned
        mock_result_exist = MagicMock()
        mock_result_exist.scalar_one_or_none.return_value = new_incident
        mock_db.execute.return_value = mock_result_exist

        resp2 = client.post("/webhooks/jenkins", json=payload, headers=auth_headers)
        assert resp2.status_code == 200

        # Assert duplicate webhook does not create a second version
        history = test_bb.history(UUID("11111111-2222-3333-4444-555555555555"))
        assert len(history) == 1
        assert mock_pub.call_count == 2

    app.dependency_overrides.clear()
