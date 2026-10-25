"""Tests for GET /approvals/{incident_id}/review, CSRF protection, and TC-15 server-side enforcement.

Validates:
1. GET /approvals/{incident_id}/review returns server-rendered Jinja2 HTML with:
   - Unified report (pipeline, build, stage, taxonomy, confidence, summary, top evidence with Jenkins links)
   - Proposed diff (syntax highlighted, no external CDN dependencies)
   - OPA / Conftest policy evaluation results
   - Two buttons/forms submitting POST to /approvals/{incident_id} with signed token and CSRF token
2. CSRF protection: rejects invalid or missing CSRF tokens with 403 Forbidden
3. TC-15 edge case:
   - Server-side enforcement: if OPA check failed (policy_rejected or policy allowed=False),
     approval returns 409 Conflict unless explicit human_override=True is passed
   - Rejection is permitted without human override
4. Offline webhook testing using respx for Slack and Teams
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
import respx
import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.config import settings
from backend.db.audit_models import (
    Approval,
    AuditEvent,
    AuditEventType,
    PolicyDecision,
    ProposalStatus,
    RemediationProposal,
)
from backend.db.database import Base, get_db
from backend.db.models import Incident
from backend.main import app
from backend.notifications.csrf import generate_csrf_token, verify_csrf_token
from backend.notifications.notifier import SlackNotifier, TeamsNotifier
from backend.notifications.renderer import NotificationRenderer
from backend.notifications.tokens import generate_approval_token
from common.blackboard import BlackboardEntry, FileBlackboard


@pytest.fixture
def database(tmp_path: Path):
    path = (tmp_path / "review_test.db").as_posix()
    sync_engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(sync_engine)
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False, class_=AsyncSession)

    async def override_db():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    yield sync_engine
    app.dependency_overrides.pop(get_db, None)
    sync_engine.dispose()


@pytest.fixture
def blackboard(tmp_path: Path):
    board = FileBlackboard(tmp_path / "blackboard")
    with patch("backend.routes.approvals.get_blackboard", return_value=board):
        yield board


@pytest.fixture
def published():
    with patch(
        "backend.routes.approvals.publisher.publish_approval_decided",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock:
        yield mock


@pytest.fixture
def client(database, blackboard, published) -> TestClient:
    return TestClient(app)


def seed_incident(
    engine,
    *,
    status: str = ProposalStatus.AWAITING_APPROVAL.value,
    attempt: int = 0,
    policy_allowed: bool = True,
    diff: str = "--- a/file.py\n+++ b/file.py\n@@ -1,1 +1,1 @@\n-old_code()\n+new_code()\n",
) -> UUID:
    incident_id = uuid4()
    with Session(engine) as session:
        session.add(
            Incident(
                id=str(incident_id),
                job_name="target-app/pipeline",
                build_number=42,
                build_url="http://jenkins:8080/job/target-app/42/",
                branch="main",
                git_commit="abcdef123456",
                failed_stage="IntegrationTest",
                status="OPEN",
                event_key=f"event-{incident_id}",
                remediation_attempt=attempt,
            )
        )
        proposal = RemediationProposal(
            incident_id=str(incident_id),
            attempt=attempt,
            status=status,
            failure_type="code_test_failure",
            rationale="Fix broken connection timeout",
            diff=diff,
            changed_files=["target_app/client.py"],
            confidence=0.92,
        )
        session.add(proposal)
        session.flush()

        session.add(
            PolicyDecision(
                proposal_id=proposal.id,
                incident_id=str(incident_id),
                policy_package="jenkinsguardians/remediation",
                allowed=policy_allowed,
                violations=[] if policy_allowed else ["TC-15 test skip detected in diff"],
                warnings=["large diff warning"] if policy_allowed else [],
            )
        )
        session.commit()
    return incident_id


# ==============================================================================
# 1. GET /approvals/{incident_id}/review (Jinja2 HTML, Unified Report, No CDNs)
# ==============================================================================

def test_review_page_renders_unified_report(client, database, blackboard):
    incident_id = seed_incident(database, attempt=0, policy_allowed=True)
    blackboard.post(
        BlackboardEntry(
            incident_id=incident_id,
            section="evidence",
            key="jenkins_agent",
            author="jenkins_agent",
            payload={
                "summary": "Integration tests failed due to assertion error",
                "failure_type": "code_test_failure",
                "confidence": 0.92,
                "evidence_items": [
                    {
                        "id": "ev-1",
                        "kind": "log_excerpt",
                        "source": "jenkins_console",
                        "content": "AssertionError: 500 != 200",
                        "location": {"file": "tests/test_api.py", "line": 42},
                    }
                ],
            },
        )
    )

    resp = client.get(f"/approvals/{incident_id}/review")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    html = resp.text

    # Unified report facts
    assert "target-app/pipeline" in html
    assert "#42" in html
    assert "IntegrationTest" in html
    assert "code_test_failure" in html
    assert "92%" in html
    assert "Fix broken connection timeout" in html

    # Top evidence with Jenkins link
    assert "http://jenkins:8080/job/target-app/42/console" in html
    assert "AssertionError: 500 != 200" in html

    # Proposed diff rendering
    assert "diff-add" in html
    assert "+new_code()" in html
    assert "diff-del" in html
    assert "-old_code()" in html

    # Policy verdict
    assert "Policy Passed" in html

    # Buttons and forms
    assert f'action="/approvals/{incident_id}"' in html
    assert 'name="decision" value="approved"' in html
    assert 'name="decision" value="rejected"' in html
    assert 'name="token"' in html
    assert 'name="csrf_token"' in html

    # Zero external CDN dependencies
    assert "http://" not in html.split("<style>")[0]  # No external links before style
    assert "cdn" not in html.lower()
    assert "googleapis" not in html.lower()
    assert "unpkg" not in html.lower()


def test_review_page_shows_blocked_status_for_policy_failure(client, database):
    incident_id = seed_incident(
        database,
        status=ProposalStatus.POLICY_REJECTED.value,
        policy_allowed=False,
    )

    resp = client.get(f"/approvals/{incident_id}/review")
    assert resp.status_code == 200
    html = resp.text

    assert "Policy Blocked" in html
    assert "TC-15" in html
    assert "TC-15 test skip detected in diff" in html
    assert "Confirm Human Override for OPA Policy Failure (TC-15)" in html


# ==============================================================================
# 2. CSRF PROTECTION & FORM SUBMISSION
# ==============================================================================

def test_csrf_token_generation_and_validation():
    incident_id = str(uuid4())
    token = generate_csrf_token(incident_id)

    assert verify_csrf_token(token, incident_id) is True
    # Fails for different incident
    assert verify_csrf_token(token, str(uuid4())) is False
    # Fails for tampered token
    assert verify_csrf_token(token + "corrupt", incident_id) is False
    assert verify_csrf_token("invalid.token", incident_id) is False


def test_form_post_with_valid_token_and_csrf_succeeds(client, database, blackboard, published):
    incident_id = seed_incident(database, attempt=0, policy_allowed=True)
    token = generate_approval_token(incident_id, 0, "approved")
    csrf_token = generate_csrf_token(str(incident_id))

    resp = client.post(
        f"/approvals/{incident_id}",
        data={
            "decision": "approved",
            "reviewer": "ishita",
            "comment": "Verified fix manually",
            "token": token,
            "csrf_token": csrf_token,
        },
        headers={"Accept": "text/html"},
    )
    assert resp.status_code == 201
    assert "Remediation Approved" in resp.text
    published.assert_awaited_once()

    # Verify DB update
    with Session(database) as session:
        proposal = session.execute(select(RemediationProposal)).scalar_one()
        assert proposal.status == "approved"
        approval = session.execute(select(Approval)).scalar_one()
        assert approval.decision == "approved"
        assert approval.reviewer == "ishita"


def test_form_post_with_invalid_csrf_is_forbidden(client, database, published):
    incident_id = seed_incident(database, attempt=0, policy_allowed=True)
    token = generate_approval_token(incident_id, 0, "approved")

    resp = client.post(
        f"/approvals/{incident_id}",
        data={
            "decision": "approved",
            "reviewer": "ishita",
            "token": token,
            "csrf_token": "forged-csrf-token",
        },
    )
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]
    published.assert_not_awaited()


# ==============================================================================
# 3. TC-15 SERVER-SIDE ENFORCEMENT
# ==============================================================================

def test_tc15_approval_blocked_without_human_override(client, database, published):
    """TC-15: Server-side check blocks approval of policy_rejected proposal with 409."""
    incident_id = seed_incident(
        database,
        status=ProposalStatus.POLICY_REJECTED.value,
        policy_allowed=False,
    )
    token = generate_approval_token(incident_id, 0, "approved")
    csrf_token = generate_csrf_token(str(incident_id))

    resp = client.post(
        f"/approvals/{incident_id}",
        data={
            "decision": "approved",
            "reviewer": "ishita",
            "token": token,
            "csrf_token": csrf_token,
            # human_override not passed
        },
    )
    assert resp.status_code == 409
    assert "TC-15" in resp.json()["detail"]
    assert "Human override required" in resp.json()["detail"]
    published.assert_not_awaited()


def test_tc15_approval_succeeds_with_explicit_human_override(client, database, published):
    """TC-15: When human_override is explicitly triggered, approval is allowed."""
    incident_id = seed_incident(
        database,
        status=ProposalStatus.POLICY_REJECTED.value,
        policy_allowed=False,
    )
    token = generate_approval_token(incident_id, 0, "approved")
    csrf_token = generate_csrf_token(str(incident_id))

    resp = client.post(
        f"/approvals/{incident_id}",
        data={
            "decision": "approved",
            "reviewer": "ishita",
            "comment": "Overriding policy rule for urgent prod fix",
            "token": token,
            "csrf_token": csrf_token,
            "human_override": "true",
        },
    )
    assert resp.status_code == 201
    published.assert_awaited_once()

    with Session(database) as session:
        proposal = session.execute(select(RemediationProposal)).scalar_one()
        assert proposal.status == "approved"
        audit = session.execute(select(AuditEvent)).scalar_one()
        assert audit.payload["human_override"] is True


def test_tc15_rejection_of_policy_blocked_proposal_is_allowed(client, database, published):
    """A reviewer can always reject a policy-blocked proposal without override."""
    incident_id = seed_incident(
        database,
        status=ProposalStatus.POLICY_REJECTED.value,
        policy_allowed=False,
    )
    token = generate_approval_token(incident_id, 0, "rejected")
    csrf_token = generate_csrf_token(str(incident_id))

    resp = client.post(
        f"/approvals/{incident_id}",
        data={
            "decision": "rejected",
            "reviewer": "shreya",
            "comment": "Confirming rejection of unsafe test skip",
            "token": token,
            "csrf_token": csrf_token,
        },
    )
    assert resp.status_code == 201
    published.assert_awaited_once()

    with Session(database) as session:
        proposal = session.execute(select(RemediationProposal)).scalar_one()
        assert proposal.status == "rejected"


# ==============================================================================
# 4. RESPOX OFFLINE TESTING FOR SLACK AND TEAMS WEBHOOKS
# ==============================================================================

@respx.mock
@pytest.mark.asyncio
async def test_slack_notifier_offline_with_respx():
    webhook_url = "https://hooks.slack.com/services/OFFLINE/TEST/123"
    route = respx.post(webhook_url).respond(200, text="ok")

    notifier = SlackNotifier(webhook_url=webhook_url)
    renderer = NotificationRenderer(base_url="http://testserver")
    rendered = renderer.render(
        incident={"id": "inc-1", "job_name": "app", "build_number": 1, "build_url": "http://jenkins/1"},
        jenkins_findings={"failure_type": "code_test_failure", "confidence": 0.9, "summary": "fail"},
        proposed_fix="fix patch",
    )

    res = await notifier.send(rendered)
    assert res.success is True
    assert res.channel == "slack"
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_teams_notifier_offline_with_respx():
    webhook_url = "https://outlook.office.com/webhook/offline-teams-test"
    route = respx.post(webhook_url).respond(200, text="1")

    notifier = TeamsNotifier(webhook_url=webhook_url)
    renderer = NotificationRenderer(base_url="http://testserver")
    rendered = renderer.render(
        incident={"id": "inc-1", "job_name": "app", "build_number": 1, "build_url": "http://jenkins/1"},
        jenkins_findings={"failure_type": "code_test_failure", "confidence": 0.9, "summary": "fail"},
        proposed_fix="fix patch",
    )

    res = await notifier.send(rendered)
    assert res.success is True
    assert res.channel == "teams"
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_slack_notifier_respx_server_error():
    webhook_url = "https://hooks.slack.com/services/FAIL/TEST/503"
    respx.post(webhook_url).respond(503, text="Service Unavailable")

    notifier = SlackNotifier(webhook_url=webhook_url)
    renderer = NotificationRenderer(base_url="http://testserver")
    rendered = renderer.render(
        incident={"id": "inc-1", "job_name": "app", "build_number": 1, "build_url": "http://jenkins/1"},
        jenkins_findings={"failure_type": "code_test_failure", "confidence": 0.9, "summary": "fail"},
        proposed_fix="fix patch",
    )

    res = await notifier.send(rendered)
    assert res.success is False
    assert "HTTP 503" in (res.error or "")
