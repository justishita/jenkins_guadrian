"""Comprehensive test suite for backend/notifications.

Tests:
1. Notifier implementations: SlackNotifier, TeamsNotifier, EmailNotifier, ConsoleNotifier
2. Dispatcher retry with backoff, provider down fallthrough, delivery status recording
3. Message renderer:
   - title (job, build, failure type, confidence)
   - 3-line summary
   - top evidence with Jenkins build URLs
   - proposed fix summary
   - signed expiring Approve/Reject URLs
   - secrets redaction
   - message length limits
4. Signed tokens:
   - constant-time HMAC signature check
   - link tampering detection
   - 24-hour expiration
   - single-use enforcement (reviewer clicking Approve twice)
5. Event consumer:
   - 'approval.requested' consumption
   - deduplication by (incident_id, attempt)
6. Approval API endpoint:
   - Approve / Reject handling
   - Tampered token (400)
   - Expired token (410)
   - Double-click / reuse (409)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
import httpx

from backend.config import settings
from backend.db.audit_models import Approval, AuditEvent, ProposalStatus, RemediationProposal
from backend.db.database import get_db
from backend.db.models import Incident
from backend.main import app
from backend.notifications.consumer import ApprovalRequestedConsumer
from backend.notifications.notifier import (
    ConsoleNotifier,
    DeliveryReport,
    EmailNotifier,
    NotificationDispatcher,
    NotificationResult,
    SlackNotifier,
    TeamsNotifier,
)
from backend.notifications.renderer import NotificationRenderer, RenderedNotification
from backend.notifications.tokens import (
    ExpiredTokenError,
    TamperedTokenError,
    TokenAlreadyUsedError,
    TokenStore,
    build_action_urls,
    consume_approval_token,
    generate_approval_token,
    verify_approval_token,
)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def sample_incident_data():
    return {
        "id": "e8d7c6b5-a4b3-4c2d-1e0f-9a8b7c6d5e4f",
        "job_name": "target-app/pipeline",
        "build_number": 42,
        "build_url": "http://jenkins.example.com/job/target-app/42/",
        "branch": "main",
        "git_commit": "abc1234def5678",
        "failed_stage": "IntegrationTests",
    }


@pytest.fixture
def sample_jenkins_findings():
    return {
        "failure_type": "code_test_failure",
        "confidence": 0.88,
        "summary": "Order controller integration tests failed with 500 Internal Server Error",
        "evidence_items": [
            {
                "id": "ev-1",
                "kind": "log_excerpt",
                "source": "jenkins_console",
                "content": "AssertionError: Expected 200 OK but received 500 with token: ghp_1234567890abcdefghijklmnopqrstuvwxyz",
                "location": {"file": "tests/test_orders.py", "line": 84},
            },
            {
                "id": "ev-2",
                "kind": "log_excerpt",
                "source": "jenkins_console",
                "content": "DatabaseConnectionTimeout: connection pool exhausted with Bearer secret_admin_token_xyz123",
            },
        ],
    }


@pytest.fixture
def sample_proposed_fix():
    return {
        "rationale": "Fix connection pool exhaustion and update timeout config",
        "changed_files": ["config/database.py", "services/order.py"],
        "diff": "--- a/config/database.py\n+++ b/config/database.py\n@@ -10,1 +10,1 @@\n-pool_size = 5\n+pool_size = 20",
    }


# ==============================================================================
# 1. NOTIFIER TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_slack_notifier_success(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    renderer = NotificationRenderer(base_url="http://testserver", signing_secret="test-secret")
    notification = renderer.render(sample_incident_data, sample_jenkins_findings, sample_proposed_fix)

    notifier = SlackNotifier(webhook_url="https://hooks.slack.com/services/T00/B00/X00")

    mock_resp = httpx.Response(200, request=httpx.Request("POST", notifier.webhook_url))
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)) as mock_post:
        result = await notifier.send(notification)
        assert result.success is True
        assert result.channel == "slack"
        mock_post.assert_called_once()
        sent_payload = mock_post.call_args.kwargs["json"]
        assert sent_payload["text"] == notification.title
        assert "blocks" in sent_payload


@pytest.mark.asyncio
async def test_slack_notifier_http_error(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    renderer = NotificationRenderer()
    notification = renderer.render(sample_incident_data, sample_jenkins_findings, sample_proposed_fix)

    notifier = SlackNotifier(webhook_url="https://hooks.slack.com/services/bad")
    mock_resp = httpx.Response(500, text="Internal Server Error", request=httpx.Request("POST", notifier.webhook_url))
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)):
        result = await notifier.send(notification)
        assert result.success is False
        assert "HTTP 500" in (result.error or "")


@pytest.mark.asyncio
async def test_slack_notifier_missing_url(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    renderer = NotificationRenderer()
    notification = renderer.render(sample_incident_data, sample_jenkins_findings, sample_proposed_fix)

    notifier = SlackNotifier(webhook_url="")
    result = await notifier.send(notification)
    assert result.success is False
    assert "not configured" in (result.error or "")


@pytest.mark.asyncio
async def test_teams_notifier_success(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    renderer = NotificationRenderer()
    notification = renderer.render(sample_incident_data, sample_jenkins_findings, sample_proposed_fix)

    notifier = TeamsNotifier(webhook_url="https://outlook.office.com/webhook/test")
    mock_resp = httpx.Response(200, request=httpx.Request("POST", notifier.webhook_url))
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)):
        result = await notifier.send(notification)
        assert result.success is True
        assert result.channel == "teams"


@pytest.mark.asyncio
async def test_teams_notifier_missing_url(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    renderer = NotificationRenderer()
    notification = renderer.render(sample_incident_data, sample_jenkins_findings, sample_proposed_fix)

    notifier = TeamsNotifier(webhook_url="")
    result = await notifier.send(notification)
    assert result.success is False
    assert "not configured" in (result.error or "")


@pytest.mark.asyncio
async def test_email_notifier_success(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    renderer = NotificationRenderer()
    notification = renderer.render(sample_incident_data, sample_jenkins_findings, sample_proposed_fix)

    notifier = EmailNotifier(
        host="smtp.example.com",
        port=587,
        recipient="oncall@example.com",
        sender="alerts@example.com",
    )

    with patch("aiosmtplib.send", new=AsyncMock(return_value=({}, "OK"))) as mock_send:
        result = await notifier.send(notification)
        assert result.success is True
        assert result.channel == "email"
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert msg["Subject"] == notification.title
        assert msg["To"] == "oncall@example.com"


@pytest.mark.asyncio
async def test_email_notifier_failure(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    renderer = NotificationRenderer()
    notification = renderer.render(sample_incident_data, sample_jenkins_findings, sample_proposed_fix)

    notifier = EmailNotifier(host="smtp.example.com", recipient="oncall@example.com")
    with patch("aiosmtplib.send", new=AsyncMock(side_effect=ConnectionRefusedError("SMTP unreachable"))):
        result = await notifier.send(notification)
        assert result.success is False
        assert "SMTP unreachable" in (result.error or "")


@pytest.mark.asyncio
async def test_console_notifier_fallback(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    renderer = NotificationRenderer()
    notification = renderer.render(sample_incident_data, sample_jenkins_findings, sample_proposed_fix)

    notifier = ConsoleNotifier()
    result = await notifier.send(notification)
    assert result.success is True
    assert result.channel == "console"


# ==============================================================================
# 2. DISPATCHER RETRY, BACKOFF & CHANNEL FALLTHROUGH
# ==============================================================================

@pytest.mark.asyncio
async def test_dispatcher_fallthrough_on_provider_down(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    """When primary channels (Slack, Teams, Email) are down, dispatcher retries with backoff and falls through to Console."""
    renderer = NotificationRenderer()
    notification = renderer.render(sample_incident_data, sample_jenkins_findings, sample_proposed_fix)

    slack_mock = MagicMock(spec=SlackNotifier)
    slack_mock.channel_name = "slack"
    slack_mock.send = AsyncMock(return_value=NotificationResult(channel="slack", success=False, error="Slack outage 503"))

    teams_mock = MagicMock(spec=TeamsNotifier)
    teams_mock.channel_name = "teams"
    teams_mock.send = AsyncMock(return_value=NotificationResult(channel="teams", success=False, error="Teams timeout"))

    console_mock = MagicMock(spec=ConsoleNotifier)
    console_mock.channel_name = "console"
    console_mock.send = AsyncMock(return_value=NotificationResult(channel="console", success=True))

    dispatcher = NotificationDispatcher(
        channels=[slack_mock, teams_mock, console_mock],
        max_retries=2,
        base_backoff=0.01,  # fast backoff for test speed
    )

    with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        report = await dispatcher.dispatch(notification)

        assert report.succeeded is True
        assert report.final_channel == "console"
        # Slack attempted 2 times (failed), Teams attempted 2 times (failed), Console attempted 1 time (succeeded)
        assert slack_mock.send.call_count == 2
        assert teams_mock.send.call_count == 2
        assert console_mock.send.call_count == 1
        assert len(report.results) == 5
        # Verify backoff was invoked for failed attempts
        assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_dispatcher_all_channels_fail(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    renderer = NotificationRenderer()
    notification = renderer.render(sample_incident_data, sample_jenkins_findings, sample_proposed_fix)

    slack_mock = MagicMock(spec=SlackNotifier)
    slack_mock.channel_name = "slack"
    slack_mock.send = AsyncMock(return_value=NotificationResult(channel="slack", success=False, error="fail"))

    dispatcher = NotificationDispatcher(channels=[slack_mock], max_retries=2, base_backoff=0.01)
    with patch("asyncio.sleep", new=AsyncMock()):
        report = await dispatcher.dispatch(notification)
        assert report.succeeded is False
        assert report.final_channel is None


# ==============================================================================
# 3. MESSAGE RENDERER: CONTENT, REDACTION & LENGTH LIMITS
# ==============================================================================

def test_renderer_title_and_summary(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    renderer = NotificationRenderer(base_url="http://jenkinsguardians.test", signing_secret="secret123")
    rendered = renderer.render(sample_incident_data, sample_jenkins_findings, sample_proposed_fix, attempt=1)

    # Title: job, build, failure type, confidence
    assert "[target-app/pipeline #42]" in rendered.title
    assert "code_test_failure" in rendered.title
    assert "88% confidence" in rendered.title

    # 3-line summary
    lines = rendered.summary_3_lines.strip().split("\n")
    assert len(lines) == 3
    assert "Pipeline target-app/pipeline #42 failed in stage 'IntegrationTests'" in lines[0]
    assert "Diagnosis (88%):" in lines[1]
    assert "Proposed fix: Fix connection pool exhaustion" in lines[2]


def test_renderer_top_evidence_links_to_jenkins(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    renderer = NotificationRenderer(base_url="http://jenkinsguardians.test", signing_secret="secret123")
    rendered = renderer.render(sample_incident_data, sample_jenkins_findings, sample_proposed_fix)

    assert len(rendered.top_evidence) == 2
    for ev in rendered.top_evidence:
        assert ev.build_url.startswith("http://jenkins.example.com/job/target-app/42")
        assert "console" in ev.build_url


def test_renderer_redacts_secrets_in_evidence(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    """Ensures secrets in evidence items, summaries, and diffs are redacted again before sending."""
    renderer = NotificationRenderer(base_url="http://jenkinsguardians.test", signing_secret="secret123")
    rendered = renderer.render(sample_incident_data, sample_jenkins_findings, sample_proposed_fix)

    # Secret ghp_ token and Bearer token must NOT appear anywhere in rendered content
    assert "ghp_" not in rendered.plain_text
    assert "secret_admin_token" not in rendered.plain_text
    assert "[REDACTED]" in rendered.plain_text

    # Check Slack payload
    slack_json = json.dumps(rendered.slack_payload)
    assert "ghp_" not in slack_json
    assert "secret_admin_token" not in slack_json

    # Check Teams payload
    teams_json = json.dumps(rendered.teams_payload)
    assert "ghp_" not in teams_json
    assert "secret_admin_token" not in teams_json

    # Check HTML Email
    assert "ghp_" not in rendered.email_html
    assert "secret_admin_token" not in rendered.email_html


def test_renderer_enforces_length_limits(sample_incident_data, sample_jenkins_findings):
    """Giant log excerpts and diffs are truncated cleanly."""
    giant_log = "FATAL ERROR OCCURRED: " + ("large_stack_trace_line_in_log\n" * 500)
    giant_diff = "--- a/file.py\n+++ b/file.py\n" + ("+ line of changed code\n" * 500)

    findings = dict(sample_jenkins_findings)
    findings["evidence_items"] = [
        {"id": "ev-big", "kind": "log_excerpt", "source": "jenkins", "content": giant_log}
    ]
    fix = {"rationale": "Large patch", "diff": giant_diff}

    renderer = NotificationRenderer()
    rendered = renderer.render(sample_incident_data, findings, fix)

    # Excerpt should be bounded
    assert len(rendered.top_evidence[0].excerpt) <= 1050
    assert "... [truncated]" in rendered.top_evidence[0].excerpt

    # Proposed fix summary should be bounded
    assert len(rendered.proposed_fix_summary) <= 1600
    assert "... [truncated]" in rendered.proposed_fix_summary


# ==============================================================================
# 4. SIGNED EXPIRING ACTION TOKENS & EDGE CASES
# ==============================================================================

def test_token_generation_and_verification():
    incident_id = str(uuid4())
    attempt = 2
    secret = "my-secure-signing-secret"

    token = generate_approval_token(incident_id, attempt, "approved", secret=secret)
    payload = verify_approval_token(token, secret=secret)

    assert payload.incident_id == incident_id
    assert payload.attempt == attempt
    assert payload.decision == "approved"
    assert not payload.is_expired


def test_token_tampering_detected():
    incident_id = str(uuid4())
    secret = "my-secure-signing-secret"
    token = generate_approval_token(incident_id, 0, "rejected", secret=secret)

    # Tamper with signature
    parts = token.split(".")
    tampered_sig = parts[0] + "." + parts[1][:-2] + "AA"
    with pytest.raises(TamperedTokenError):
        verify_approval_token(tampered_sig, secret=secret)

    # Tamper with payload (e.g. changing decision from rejected to approved)
    with pytest.raises(TamperedTokenError):
        # Decode, change payload, re-encode without key
        import base64
        raw = base64.urlsafe_b64decode(parts[0] + "==").decode("utf-8")
        tampered_json = raw.replace('"rejected"', '"approved"')
        tampered_b64 = base64.urlsafe_b64encode(tampered_json.encode("utf-8")).decode("utf-8").rstrip("=")
        verify_approval_token(f"{tampered_b64}.{parts[1]}", secret=secret)


def test_token_expired_detected():
    incident_id = str(uuid4())
    secret = "my-secure-signing-secret"
    # Token expired 10 seconds ago
    token = generate_approval_token(incident_id, 0, "approved", secret=secret, expires_in_seconds=-10)

    with pytest.raises(ExpiredTokenError):
        verify_approval_token(token, secret=secret)


def test_token_single_use_detected():
    incident_id = str(uuid4())
    secret = "my-secure-signing-secret"
    token = generate_approval_token(incident_id, 0, "approved", secret=secret)
    store = TokenStore()

    # First consumption succeeds
    payload = consume_approval_token(token, secret=secret, store=store)
    assert payload.decision == "approved"

    # Second consumption (reviewer clicking Approve twice) must fail
    with pytest.raises(TokenAlreadyUsedError):
        consume_approval_token(token, secret=secret, store=store)


def test_build_action_urls():
    incident_id = str(uuid4())
    urls = build_action_urls(incident_id, 0, base_url="https://guardian.company.com", secret="signing-key")
    assert urls["approve_url"].startswith("https://guardian.company.com/approvals/decide?decision=approved&token=")
    assert urls["reject_url"].startswith("https://guardian.company.com/approvals/decide?decision=rejected&token=")


# ==============================================================================
# 5. APPROVAL CONSUMER & DEDUPLICATION
# ==============================================================================

@pytest.mark.asyncio
async def test_consumer_deduplicates_duplicate_events(sample_incident_data, sample_jenkins_findings, sample_proposed_fix):
    incident_id = sample_incident_data["id"]
    attempt = 0

    mock_dispatcher = MagicMock(spec=NotificationDispatcher)
    mock_dispatcher.dispatch = AsyncMock(
        return_value=DeliveryReport(
            incident_id=incident_id,
            attempt=attempt,
            succeeded=True,
            final_channel="slack",
            results=[NotificationResult(channel="slack", success=True)],
        )
    )

    consumer = ApprovalRequestedConsumer(notification_dispatcher=mock_dispatcher)
    consumer.reset_dedup()

    event_payload = {
        "incident_id": incident_id,
        "attempt": attempt,
        "incident": sample_incident_data,
        "jenkins_findings": sample_jenkins_findings,
        "proposed_fix": sample_proposed_fix,
    }

    # First event should process and dispatch
    with patch.object(consumer, "_record_delivery_audit", new=AsyncMock()):
        report1 = await consumer.handle_approval_request(event_payload)
        assert report1 is not None
        assert report1.succeeded is True
        assert mock_dispatcher.dispatch.call_count == 1

        # Second event with same (incident_id, attempt) must be ignored as duplicate
        report2 = await consumer.handle_approval_request(event_payload)
        assert report2 is None
        # Dispatcher must NOT be called again
        assert mock_dispatcher.dispatch.call_count == 1

        # Different attempt for same incident should process
        event_payload_attempt_1 = dict(event_payload)
        event_payload_attempt_1["attempt"] = 1
        report3 = await consumer.handle_approval_request(event_payload_attempt_1)
        assert report3 is not None
        assert mock_dispatcher.dispatch.call_count == 2


# ==============================================================================
# 6. APPROVAL ROUTE (/approvals/decide)
# ==============================================================================

def test_decide_approval_endpoint_success(client):
    incident_id = str(uuid4())
    attempt = 0
    token = generate_approval_token(incident_id, attempt, "approved")

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    mock_scalars = MagicMock()
    mock_scalars.first.return_value = None
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    try:
        with patch("backend.routes.approvals.publisher.publish_event", new=AsyncMock(return_value=True)):
            resp = client.get(
                f"/approvals/decide?decision=approved&token={token}&reviewer=ishita",
                headers={"Accept": "application/json"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert data["decision"] == "approved"
            assert data["reviewer"] == "ishita"
    finally:
        app.dependency_overrides.clear()


def test_decide_approval_tampered_token_returns_400(client):
    incident_id = str(uuid4())
    token = generate_approval_token(incident_id, 0, "approved")
    tampered_token = token + "corrupted"

    resp = client.get(
        f"/approvals/decide?decision=approved&token={tampered_token}",
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 400
    assert "tampered" in resp.json()["detail"].lower() or "signature" in resp.json()["detail"].lower()


def test_decide_approval_expired_token_returns_410(client):
    incident_id = str(uuid4())
    expired_token = generate_approval_token(incident_id, 0, "approved", expires_in_seconds=-5)

    resp = client.get(
        f"/approvals/decide?decision=approved&token={expired_token}",
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 410
    assert "expired" in resp.json()["detail"].lower()


def test_decide_approval_twice_returns_409(client):
    """Reviewer clicking Approve twice returns 409 Conflict."""
    incident_id = str(uuid4())
    token = generate_approval_token(incident_id, 0, "approved")

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    mock_scalars = MagicMock()
    mock_scalars.first.return_value = None
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    try:
        with patch("backend.routes.approvals.publisher.publish_event", new=AsyncMock(return_value=True)):
            # First click
            resp1 = client.get(
                f"/approvals/decide?decision=approved&token={token}",
                headers={"Accept": "application/json"},
            )
            assert resp1.status_code == 200

            # Second click with same single-use token
            resp2 = client.get(
                f"/approvals/decide?decision=approved&token={token}",
                headers={"Accept": "application/json"},
            )
            assert resp2.status_code == 409
            assert "already been used" in resp2.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_decide_approval_html_rendering(client):
    incident_id = str(uuid4())
    token = generate_approval_token(incident_id, 0, "approved")

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    mock_scalars = MagicMock()
    mock_scalars.first.return_value = None
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    try:
        with patch("backend.routes.approvals.publisher.publish_event", new=AsyncMock(return_value=True)):
            resp = client.get(
                f"/approvals/decide?decision=approved&token={token}",
                headers={"Accept": "text/html"},
            )
            assert resp.status_code == 200
            assert "text/html" in resp.headers["content-type"]
            assert "Remediation Approved" in resp.text
    finally:
        app.dependency_overrides.clear()
