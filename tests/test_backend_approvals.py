"""Tests for the shared approval contract (``backend/routes/approvals.py``).

**Owner:** P3. Run against a real SQLite database rather than mocks, because the
guarantees that matter - one decision per attempt, the proposal's status moving with
it, the audit row written in the same transaction - live in the database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest
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
from common.blackboard import BlackboardEntry, FileBlackboard


TOKEN = "approval-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
	path = (tmp_path / "approvals.db").as_posix()
	sync_engine = create_engine(f"sqlite:///{path}")
	Base.metadata.create_all(sync_engine)
	# NullPool: TestClient runs the app on its own event loop, so no connection may
	# outlive the request that opened it.
	async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)
	sessions = async_sessionmaker(async_engine, expire_on_commit=False, class_=AsyncSession)

	async def override_db():
		async with sessions() as session:
			yield session

	app.dependency_overrides[get_db] = override_db
	monkeypatch.setattr(settings, "APPROVAL_API_TOKEN", TOKEN)
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


def seed(engine, *, status: str = ProposalStatus.AWAITING_APPROVAL.value, attempt: int = 0) -> UUID:
	incident_id = uuid4()
	with Session(engine) as session:
		session.add(
			Incident(
				id=str(incident_id),
				job_name="target-app/main",
				build_number=7,
				build_url="http://jenkins:8080/job/target-app/7/",
				branch="main",
				git_commit="abc1234",
				failed_stage="Test",
				status="OPEN",
				event_key=f"event-{incident_id}",
				remediation_attempt=attempt,
			)
		)
		proposal = RemediationProposal(
			incident_id=str(incident_id),
			attempt=attempt,
			status=status,
			failure_type="dependency_regression",
			rationale="Revert requests to the last version that resolved.",
			diff="-requests==99.0.0\n+requests==2.32.3\n",
			changed_files=["target_app/requirements.txt"],
			confidence=0.85,
		)
		session.add(proposal)
		session.flush()
		session.add(
			PolicyDecision(
				proposal_id=proposal.id,
				incident_id=str(incident_id),
				policy_package="jenkinsguardians/remediation",
				allowed=True,
				violations=[],
				warnings=[],
			)
		)
		session.commit()
	return incident_id


def approve(client: TestClient, incident_id: UUID, **body):
	return client.post(
		f"/approvals/{incident_id}",
		json={"decision": "approved", "reviewer": "ishita", "comment": "", **body},
		headers=AUTH,
	)


# --- auth ------------------------------------------------------------------------


def test_requests_without_the_token_are_refused(client, database) -> None:
	incident_id = seed(database)
	assert client.get(f"/approvals/{incident_id}").status_code == 401
	assert client.post(f"/approvals/{incident_id}", json={"decision": "approved", "reviewer": "x"}).status_code == 401
	assert client.get(f"/approvals/{incident_id}", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_unset_token_disables_the_api(client, database, monkeypatch) -> None:
	incident_id = seed(database)
	monkeypatch.setattr(settings, "APPROVAL_API_TOKEN", "")
	assert client.get(f"/approvals/{incident_id}", headers={"Authorization": "Bearer "}).status_code == 401
	assert approve(client, incident_id).status_code == 401


# --- GET -------------------------------------------------------------------------


def test_get_shows_fix_policy_and_evidence(client, database, blackboard) -> None:
	incident_id = seed(database, attempt=1)
	blackboard.post(
		BlackboardEntry(
			incident_id=incident_id,
			section="evidence",
			key="code_agent",
			author="code_agent",
			payload={"summary": "requests pinned to a version that does not exist"},
		)
	)

	response = client.get(f"/approvals/{incident_id}", headers=AUTH)

	assert response.status_code == 200
	view = response.json()
	assert view["awaiting_decision"] is True
	assert view["fix"]["diff"].startswith("-requests==99.0.0")
	assert view["fix"]["branch"] == f"ai-fix/{incident_id}-1"
	assert view["policy_decisions"][0]["allowed"] is True
	assert view["evidence"]["code_agent"]["summary"].startswith("requests pinned")
	assert view["approval"] is None


def test_get_unknown_incident_is_404(client) -> None:
	assert client.get(f"/approvals/{uuid4()}", headers=AUTH).status_code == 404


def test_get_rejects_a_non_uuid_incident(client) -> None:
	assert client.get("/approvals/not-a-uuid", headers=AUTH).status_code == 422


# --- POST ------------------------------------------------------------------------


def test_approval_is_recorded_audited_and_announced(client, database, blackboard, published) -> None:
	incident_id = seed(database)

	response = approve(client, incident_id, comment="Port 0 is clearly wrong.")

	assert response.status_code == 201
	assert response.json()["event_published"] is True
	with Session(database) as session:
		row = session.execute(select(Approval)).scalar_one()
		assert (row.incident_id, row.attempt, row.decision, row.reviewer, row.comment) == (
			str(incident_id), 0, "approved", "ishita", "Port 0 is clearly wrong.",
		)
		assert row.decided_at is not None
		assert session.execute(select(RemediationProposal)).scalar_one().status == "approved"
		audit = session.execute(select(AuditEvent)).scalar_one()
		assert audit.event_type == AuditEventType.APPROVAL_DECIDED.value
		assert audit.payload["reviewer"] == "ishita"

	event = published.await_args.args[0]
	assert event["decision"] == "approved"
	assert event["branch"] == f"ai-fix/{incident_id}-0"
	assert blackboard.read(incident_id, "approval", key="attempt_0").payload["reviewer"] == "ishita"


def test_rejection_marks_the_proposal_rejected(client, database, published) -> None:
	"""TC-16: a rejection is recorded and announced, and nothing else happens here."""
	incident_id = seed(database)

	response = approve(client, incident_id, decision="rejected", reviewer="shreya", comment="Wrong port.")

	assert response.status_code == 201
	with Session(database) as session:
		assert session.execute(select(RemediationProposal)).scalar_one().status == "rejected"
	assert published.await_args.args[0]["decision"] == "rejected"


def test_a_second_decision_on_the_same_attempt_is_refused(client, database) -> None:
	incident_id = seed(database)
	assert approve(client, incident_id).status_code == 201

	again = approve(client, incident_id, decision="rejected", reviewer="shreya")

	assert again.status_code == 409
	assert "already approved by ishita" in again.json()["detail"]
	with Session(database) as session:
		assert len(session.execute(select(Approval)).scalars().all()) == 1


def test_the_database_refuses_a_duplicate_decision(database) -> None:
	"""The race the endpoint cannot see: the unique constraint is the last word."""
	incident_id = seed(database)
	with Session(database) as session:
		proposal_id = session.execute(select(RemediationProposal.id)).scalar_one()
		for reviewer in ("ishita", "shreya"):
			session.add(
				Approval(proposal_id=proposal_id, incident_id=str(incident_id), attempt=0, decision="approved", reviewer=reviewer)
			)
		with pytest.raises(Exception, match="UNIQUE"):
			session.commit()


@pytest.mark.parametrize(
	"status",
	[ProposalStatus.POLICY_REJECTED.value, ProposalStatus.DRAFT.value, ProposalStatus.PR_CREATED.value],
)
def test_only_a_proposal_awaiting_approval_can_be_decided(client, database, published, status) -> None:
	"""TC-15: a fix the policy blocked never reaches a human decision."""
	incident_id = seed(database, status=status)

	response = approve(client, incident_id)

	assert response.status_code == 409
	published.assert_not_awaited()


def test_incident_without_a_proposal_cannot_be_decided(client, database) -> None:
	incident_id = seed(database)
	with Session(database) as session:
		session.query(PolicyDecision).delete()
		session.query(RemediationProposal).delete()
		session.commit()
	assert approve(client, incident_id).status_code == 409


@pytest.mark.parametrize(
	"body",
	[
		{"decision": "maybe", "reviewer": "ishita"},
		{"decision": "approved", "reviewer": ""},
		{"decision": "approved", "reviewer": "   "},
		{"decision": "approved"},
		{"decision": "approved", "reviewer": "ishita", "auto_merge": True},
	],
)
def test_malformed_decisions_are_rejected(client, database, published, body) -> None:
	incident_id = seed(database)
	response = client.post(f"/approvals/{incident_id}", json=body, headers=AUTH)
	assert response.status_code == 422
	published.assert_not_awaited()


def test_secrets_in_the_comment_are_redacted(client, database) -> None:
	incident_id = seed(database)
	approve(client, incident_id, comment="ok, token=ghp_abcdefghijklmnopqrstuvwxyz0123456789")
	with Session(database) as session:
		comment = session.execute(select(Approval.comment)).scalar_one()
	assert "ghp_" not in comment
	assert "[REDACTED]" in comment


def test_failed_publish_is_reported_but_the_decision_stands(client, database, published) -> None:
	published.return_value = False
	incident_id = seed(database)

	response = approve(client, incident_id)

	assert response.status_code == 201
	assert response.json()["event_published"] is False
	with Session(database) as session:
		assert session.execute(select(Approval.decision)).scalar_one() == "approved"


def test_decision_applies_to_the_latest_attempt(client, database) -> None:
	incident_id = seed(database, status=ProposalStatus.REJECTED.value, attempt=0)
	with Session(database) as session:
		session.add(
			RemediationProposal(
				incident_id=str(incident_id),
				attempt=1,
				status=ProposalStatus.AWAITING_APPROVAL.value,
				diff="",
			)
		)
		session.commit()

	response = approve(client, incident_id)

	assert response.status_code == 201
	assert response.json()["approval"]["attempt"] == 1
	assert response.json()["branch"] == f"ai-fix/{incident_id}-1"
