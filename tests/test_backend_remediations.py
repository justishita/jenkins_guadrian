"""Tests for ``backend/routes/remediations.py``: the Coordinator registering fixes.

**Owner:** P3. Against a real SQLite database: the guarantees - one proposal per incident
attempt, a reviewer asked only for a fix policy allowed, never asked twice - live there.
"""

from __future__ import annotations

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
from backend.db.audit_models import PolicyDecision, RemediationProposal
from backend.db.database import Base, get_db
from backend.db.models import Incident
from backend.main import app


TOKEN = "remediation-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
	path = (tmp_path / "remediations.db").as_posix()
	sync_engine = create_engine(f"sqlite:///{path}")
	Base.metadata.create_all(sync_engine)
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
def requested():
	with patch(
		"backend.routes.remediations.publisher.publish_approval_requested", new_callable=AsyncMock, return_value=True
	) as mock:
		yield mock


@pytest.fixture
def client(database, requested) -> TestClient:
	return TestClient(app)


def seed(engine) -> UUID:
	incident_id = uuid4()
	with Session(engine) as session:
		session.add(
			Incident(
				id=str(incident_id),
				job_name="target-app/main",
				build_number=42,
				build_url="http://jenkins:8080/job/target-app/42/",
				branch="main",
				git_commit="a1b2c3d4",
				failed_stage="Setup",
				status="OPEN",
				event_key=f"event-{incident_id}",
			)
		)
		session.commit()
	return incident_id


def registration(incident_id: UUID, *, allowed: bool = True, attempt: int = 0) -> dict:
	return {
		"incident_id": str(incident_id),
		"attempt": attempt,
		"failure_type": "dependency_regression",
		"confidence": 0.9,
		"rationale": "Pin httpx back to the last good version.",
		"diff": "--- a/target_app/requirements.txt\n+++ b/target_app/requirements.txt\n@@\n-httpx==0.99.0\n+httpx==0.28.1\n",
		"changed_files": ["target_app/requirements.txt"],
		"policy": {"allowed": allowed, "violations": [] if allowed else ["proposal disables a test"], "warnings": []},
	}


def test_registration_needs_the_token(client, database) -> None:
	incident_id = seed(database)
	assert client.post("/remediations", json=registration(incident_id)).status_code == 401


def test_an_allowed_fix_is_stored_and_a_reviewer_is_asked(client, database, requested) -> None:
	incident_id = seed(database)

	response = client.post("/remediations", json=registration(incident_id), headers=AUTH)

	assert response.status_code == 201
	assert response.json()["approval_requested"] is True
	with Session(database) as session:
		proposal = session.execute(select(RemediationProposal)).scalar_one()
		assert (proposal.status, proposal.attempt) == ("awaiting_approval", 0)
		assert session.execute(select(PolicyDecision.allowed)).scalar_one() is True
	message = requested.await_args.args[0]
	assert message["incident_id"] == str(incident_id)
	assert message["branch"] == f"ai-fix/{incident_id}-0"
	assert "-httpx==0.99.0" in message["proposed_fix"]["diff"]


def test_a_retried_registration_never_asks_twice(client, database, requested) -> None:
	incident_id = seed(database)
	client.post("/remediations", json=registration(incident_id), headers=AUTH)

	again = client.post("/remediations", json=registration(incident_id), headers=AUTH)

	assert again.status_code == 200 and again.json()["created"] is False
	requested.assert_awaited_once()
	with Session(database) as session:
		assert len(session.execute(select(RemediationProposal)).scalars().all()) == 1


def test_a_denied_fix_is_recorded_but_no_one_is_asked(client, database, requested) -> None:
	"""TC-15: the blocked proposal is kept for the audit, and never reaches a reviewer."""
	incident_id = seed(database)

	response = client.post("/remediations", json=registration(incident_id, allowed=False), headers=AUTH)

	assert response.status_code == 201
	requested.assert_not_awaited()
	with Session(database) as session:
		assert session.execute(select(RemediationProposal.status)).scalar_one() == "policy_rejected"
		assert session.execute(select(PolicyDecision.violations)).scalar_one() == ["proposal disables a test"]


def test_an_unknown_incident_is_404(client) -> None:
	assert client.post("/remediations", json=registration(uuid4()), headers=AUTH).status_code == 404


def test_the_opened_pull_request_is_recorded(client, database) -> None:
	incident_id = seed(database)
	client.post("/remediations", json=registration(incident_id), headers=AUTH)

	response = client.patch(
		f"/remediations/{incident_id}/0",
		json={"status": "pr_created", "pull_request_url": "https://github.com/o/r/pull/7"},
		headers=AUTH,
	)

	assert response.status_code == 200
	with Session(database) as session:
		proposal = session.execute(select(RemediationProposal)).scalar_one()
		assert (proposal.status, proposal.pull_request_url) == ("pr_created", "https://github.com/o/r/pull/7")


def test_an_outcome_for_an_unknown_proposal_is_404(client, database) -> None:
	incident_id = seed(database)
	response = client.patch(f"/remediations/{incident_id}/0", json={"status": "failed"}, headers=AUTH)
	assert response.status_code == 404
