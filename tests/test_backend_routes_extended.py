"""Extended unit tests for backend/routes (webhooks and validation)."""

from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from backend.main import app
import backend.routes.webhooks as wh_module
import backend.routes.validation as val_module


@pytest.fixture
def client():
    return TestClient(app)


def test_webhook_secret_not_configured_returns_401(client, monkeypatch):
    """When WEBHOOK_SHARED_SECRET is empty, webhook authentication fails with 401."""
    monkeypatch.setattr(wh_module.settings, "WEBHOOK_SHARED_SECRET", "")
    resp = client.post(
        "/webhooks/jenkins",
        headers={"X-Webhook-Token": "some-token"},
        json={"job_name": "app", "build_number": 1, "build_url": "http://j", "branch": "m", "git_commit": "c"},
    )
    assert resp.status_code == 401


def test_webhook_body_size_limit_exceeded(client, monkeypatch):
    """Body exceeding 64KB raises 413."""
    monkeypatch.setattr(wh_module.settings, "WEBHOOK_SHARED_SECRET", "valid-secret")
    huge_data = "a" * (65 * 1024)
    resp = client.post(
        "/webhooks/jenkins",
        headers={"X-Webhook-Token": "valid-secret", "Content-Length": str(len(huge_data))},
        content=huge_data,
    )
    assert resp.status_code == 413


def test_webhook_invalid_content_length(client, monkeypatch):
    """Negative content-length returns 400."""
    monkeypatch.setattr(wh_module.settings, "WEBHOOK_SHARED_SECRET", "valid-secret")
    resp = client.post(
        "/webhooks/jenkins",
        headers={"X-Webhook-Token": "valid-secret", "Content-Length": "-5"},
        content="data",
    )
    assert resp.status_code == 400


def test_webhook_invalid_json_body(client, monkeypatch):
    """Invalid JSON payload returns 422."""
    monkeypatch.setattr(wh_module.settings, "WEBHOOK_SHARED_SECRET", "valid-secret")
    resp = client.post(
        "/webhooks/jenkins",
        headers={"X-Webhook-Token": "valid-secret"},
        content="{invalid json}",
    )
    assert resp.status_code == 422


def test_validation_unauthenticated(client, monkeypatch):
    """Validation webhook without token returns 401."""
    monkeypatch.setattr(val_module.settings, "WEBHOOK_SHARED_SECRET", "val-secret")
    resp = client.post(
        "/webhooks/jenkins/validation",
        json={"job_name": "app", "build_number": 1, "build_url": "http://j", "branch": "m", "git_commit": "c"},
    )
    assert resp.status_code == 401
