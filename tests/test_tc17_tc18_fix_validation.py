"""TC-17 and TC-18: what happens after Jenkins re-validates an approved fix.

**Owner:** P3 (approval and remediation recording, Week 7). The webhook itself is P1's;
these tests pin the outcome the plan requires, end to end through a real database:

* **TC-17 Fix Validation Failure** - the approved patch still fails: the failed
  remediation is recorded and re-investigation starts, instead of declaring success.
  Once the attempts are used up, the incident escalates to a human.
* **TC-18 Successful Remediation** - Jenkins passes: the incident is resolved and the
  proposal marked as validated, which is what the fix-success metrics count.

The run is linked to its incident by the ``ai-fix/<incident_id>-<attempt>`` branch
name alone, as Jenkins reports it.
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
from backend.db.audit_models import AuditEvent, AuditEventType, ProposalStatus, RemediationProposal
from backend.db.database import Base, get_db
from backend.db.models import Incident
from backend.main import app
from common.blackboard import FileBlackboard
from common.fix_branch import format_fix_branch


SECRET = "tc17-webhook-secret"
HEADERS = {"X-Webhook-Token": SECRET, "Content-Type": "application/json"}


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
	path = (tmp_path / "validation.db").as_posix()
	sync_engine = create_engine(f"sqlite:///{path}")
	Base.metadata.create_all(sync_engine)
	async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)
	sessions = async_sessionmaker(async_engine, expire_on_commit=False, class_=AsyncSession)

	async def override_db():
		async with sessions() as session:
			yield session

	app.dependency_overrides[get_db] = override_db
	monkeypatch.setattr(settings, "WEBHOOK_SHARED_SECRET", SECRET)
	monkeypatch.delenv("MAX_ATTEMPTS", raising=False)
	yield sync_engine
	app.dependency_overrides.pop(get_db, None)
	sync_engine.dispose()


@pytest.fixture
def published():
	with patch(
		"backend.routes.validation.publisher.publish_validation_completed", new_callable=AsyncMock, return_value=True
	) as completed, patch(
		"backend.routes.validation.publisher.publish_incident_reinvestigate", new_callable=AsyncMock, return_value=True
	) as reinvestigate:
		yield completed, reinvestigate


@pytest.fixture
def client(database, published, tmp_path: Path) -> TestClient:
	with patch("backend.routes.validation.get_blackboard", return_value=FileBlackboard(tmp_path / "blackboard")):
		yield TestClient(app)


def seed(engine, *, attempt: int = 0) -> UUID:
	"""An incident whose approved fix is out for re-validation on its ai-fix branch."""
	incident_id = uuid4()
	with Session(engine) as session:
		session.add(
			Incident(
				id=str(incident_id),
				job_name="target-app/main",
				build_number=42,
				build_url="http://jenkins:8080/job/target-app/job/main/42/",
				branch="main",
				git_commit="a1b2c3d4",
				failed_stage="Setup",
				status="OPEN",
				event_key=f"event-{incident_id}",
				remediation_attempt=attempt,
			)
		)
		session.add(
			RemediationProposal(
				incident_id=str(incident_id),
				attempt=attempt,
				status=ProposalStatus.PR_CREATED.value,
				failure_type="dependency_regression",
				rationale="Pin httpx back to 0.28.1.",
				diff="-httpx==0.99.0\n+httpx==0.28.1\n",
			)
		)
		session.commit()
	return incident_id


def validate(client: TestClient, incident_id: UUID, attempt: int, result: str, build: int):
	return client.post(
		"/webhooks/jenkins/validation",
		json={
			"job_name": "target-app",
			"build_number": build,
			"result": result,
			"branch": f"origin/{format_fix_branch(incident_id, attempt)}",
		},
		headers=HEADERS,
	)


def state(engine, incident_id: UUID) -> tuple[Incident, RemediationProposal, list[AuditEvent]]:
	with Session(engine) as session:
		incident = session.execute(select(Incident).where(Incident.id == str(incident_id))).scalar_one()
		proposal = session.execute(
			select(RemediationProposal).where(RemediationProposal.incident_id == str(incident_id))
		).scalar_one()
		events = list(
			session.execute(select(AuditEvent).where(AuditEvent.incident_id == str(incident_id))).scalars()
		)
		session.expunge_all()
		return incident, proposal, events


def test_tc17_a_fix_that_still_fails_is_recorded_and_reinvestigated(client, database, published) -> None:
	_, reinvestigate = published
	incident_id = seed(database, attempt=0)

	response = validate(client, incident_id, attempt=0, result="FAILURE", build=101)

	assert response.status_code == 202
	assert response.json()["incident_id"] == str(incident_id)  # linked by branch name alone
	incident, proposal, events = state(database, incident_id)
	assert proposal.status == ProposalStatus.VALIDATION_FAILED.value  # the failure is recorded
	assert incident.status == "OPEN"  # not declared a success
	assert incident.remediation_attempt == 1
	revalidation = [e for e in events if e.event_type == AuditEventType.REVALIDATION_COMPLETED.value]
	assert len(revalidation) == 1 and revalidation[0].ok is False
	reinvestigate.assert_awaited_once()
	message = reinvestigate.await_args.args[0]
	assert message["attempt"] == 1
	assert "Validation failed" in message["previous_failure_summary"]


def test_tc17_when_the_attempts_run_out_a_human_takes_over(client, database, published) -> None:
	_, reinvestigate = published
	incident_id = seed(database, attempt=2)  # MAX_ATTEMPTS defaults to 2

	validate(client, incident_id, attempt=2, result="FAILURE", build=102)

	incident, proposal, _ = state(database, incident_id)
	assert proposal.status == ProposalStatus.VALIDATION_FAILED.value
	assert incident.status == "ESCALATED"
	reinvestigate.assert_not_awaited()


def test_tc18_a_passing_fix_resolves_the_incident(client, database, published) -> None:
	completed, reinvestigate = published
	incident_id = seed(database, attempt=0)

	validate(client, incident_id, attempt=0, result="SUCCESS", build=103)

	incident, proposal, events = state(database, incident_id)
	assert incident.status == "RESOLVED"
	assert proposal.status == ProposalStatus.VALIDATION_PASSED.value  # counted as a fix success
	assert any(e.event_type == AuditEventType.REVALIDATION_COMPLETED.value and e.ok for e in events)
	assert completed.await_args.args[0]["status"] == "SUCCESS"
	reinvestigate.assert_not_awaited()


def test_a_branch_that_is_not_an_ai_fix_branch_links_to_nothing(client, database, published) -> None:
	_, reinvestigate = published
	incident_id = seed(database, attempt=0)

	response = client.post(
		"/webhooks/jenkins/validation",
		json={"job_name": "target-app", "build_number": 104, "result": "FAILURE", "branch": f"ai-fix/{str(incident_id)[:8]}-0"},
		headers=HEADERS,
	)

	assert response.json()["message"] == "orphan validation"
	incident, proposal, _ = state(database, incident_id)
	assert (incident.status, proposal.status) == ("OPEN", ProposalStatus.PR_CREATED.value)
	reinvestigate.assert_not_awaited()
