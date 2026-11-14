"""The full remediation loop, end to end, in one process.

**Owner:** P3 (Week 8: full integration tests). Every component here is the real one,
talking to the others the way the deployed system does:

    code agent -> blackboard -> ADK Coordinator (report) -> remediation service
      -> remediation policy (real Rego, opa CLI) -> POST /remediations (backend, real DB)
      -> approval.requested -> human decides via POST /approvals -> approval.decided
      -> remediation service -> pull-request policy -> draft PR (GitHub fake)
      -> PATCH /remediations -> Jenkins re-validates the ai-fix branch -> RESOLVED

Only the edges are simulated: GitHub (in memory), RabbitMQ (each published message is
captured and handed to its consumer), and the Jenkins and metrics evidence (recorded,
as in the demo). The backend is called over HTTP through its ASGI app.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.config import settings
from backend.db.audit_models import Approval as ApprovalRow, RemediationProposal
from backend.db.database import Base, get_db
from backend.db.models import Incident
from backend.main import app
from common.blackboard import FileBlackboard
from common.models import FailureTaxonomy
from coordinator.remediation_service import BackendUnavailable, HttpApprovalBackend, PlanStore, RemediationService
from scripts.demo_p3 import BREAKING_SHA, REQUIREMENTS, Pipeline, find_opa, incident, recorded, tc03_github


pytestmark = pytest.mark.skipif(find_opa() is None, reason="opa binary not installed (python scripts/get_opa.py)")

TOKEN = "loop-approval-token"
WEBHOOK = "loop-webhook-secret"
INCIDENT = "11111111-0303-4303-8303-0000000000ff"


class Bus:
	"""Stands in for RabbitMQ: records what the backend publishes, by routing key."""

	def __init__(self) -> None:
		self.messages: dict[str, list[dict[str, Any]]] = {}

	def publisher(self, key: str) -> AsyncMock:
		async def publish(payload: dict[str, Any]) -> bool:
			self.messages.setdefault(key, []).append(payload)
			return True

		return AsyncMock(side_effect=publish)


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
	path = (tmp_path / "loop.db").as_posix()
	engine = create_engine(f"sqlite:///{path}")
	Base.metadata.create_all(engine)
	sessions = async_sessionmaker(
		create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool), expire_on_commit=False, class_=AsyncSession
	)

	async def override_db():
		async with sessions() as session:
			yield session

	app.dependency_overrides[get_db] = override_db
	monkeypatch.setattr(settings, "APPROVAL_API_TOKEN", TOKEN)
	monkeypatch.setattr(settings, "WEBHOOK_SHARED_SECRET", WEBHOOK)
	monkeypatch.delenv("MAX_ATTEMPTS", raising=False)
	bus = Bus()
	board = FileBlackboard(tmp_path / "blackboard")
	with Session(engine) as session:
		session.add(
			Incident(
				id=INCIDENT,
				job_name="target-app/main",
				build_number=42,
				build_url="http://jenkins:8080/job/target-app/job/main/42/",
				branch="main",
				git_commit=BREAKING_SHA,
				failed_stage="Setup",
				status="OPEN",
				event_key=f"event-{INCIDENT}",
			)
		)
		session.commit()
	with patch("backend.routes.remediations.publisher.publish_approval_requested", bus.publisher("approval.requested")), \
		patch("backend.routes.approvals.publisher.publish_approval_decided", bus.publisher("approval.decided")), \
		patch("backend.routes.validation.publisher.publish_validation_completed", bus.publisher("validation.completed")), \
		patch("backend.routes.validation.publisher.publish_incident_reinvestigate", bus.publisher("incident.reinvestigate")), \
		patch("backend.routes.approvals.get_blackboard", return_value=board), \
		patch("backend.routes.validation.get_blackboard", return_value=board):
		yield engine, bus
	app.dependency_overrides.pop(get_db, None)
	engine.dispose()


def backend_http() -> httpx.AsyncClient:
	return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent-api")


async def investigate(tmp_path: Path):
	"""TC-03 up to the report: real code agent and Coordinator, recorded Jenkins and metrics."""
	github = tc03_github()
	pipeline = Pipeline(tmp_path / "work", github, find_opa())
	event = incident(INCIDENT, BREAKING_SHA, "Setup")
	await pipeline.run_code_agent(event)
	await pipeline.post_recorded(recorded(
		"jenkins_agent", event.incident_id, FailureTaxonomy.DEPENDENCY_REGRESSION, 0.65,
		"pip could not resolve httpx==0.99.0", finding="No matching distribution found for httpx==0.99.0",
	))
	await pipeline.post_recorded(recorded(
		"metrics_agent", event.incident_id, FailureTaxonomy.UNKNOWN, 0.2, "nothing anomalous", status="insufficient_evidence",
	))
	report = await pipeline.coordinator.synthesize(event.incident_id)
	assert report is not None and report.actionable
	return github, pipeline, event, report


def service_for(pipeline: Pipeline, github, tmp_path: Path, backend: Any) -> RemediationService:
	return RemediationService(pipeline.gate, github.reader, backend, PlanStore(tmp_path / "plans"), pipeline.audit)


def proposal(engine) -> RemediationProposal:
	with Session(engine) as session:
		row = session.execute(select(RemediationProposal).where(RemediationProposal.incident_id == INCIDENT)).scalar_one()
		session.expunge(row)
		return row


def incident_status(engine) -> str:
	with Session(engine) as session:
		return session.execute(select(Incident.status).where(Incident.id == INCIDENT)).scalar_one()


@pytest.mark.asyncio
async def test_approved_fix_goes_from_report_to_draft_pr_to_resolved(stack, tmp_path: Path) -> None:
	engine, bus = stack
	github, pipeline, event, report = await investigate(tmp_path)
	async with backend_http() as http:
		service = service_for(pipeline, github, tmp_path, HttpApprovalBackend("http://agent-api", TOKEN, client=http))

		# 1. Report -> proposal -> remediation policy -> registered -> a reviewer is asked.
		record = await service.on_report(event, report)
		assert record.status == "awaiting_approval"
		assert proposal(engine).status == "awaiting_approval"
		[request] = bus.messages["approval.requested"]
		assert request["branch"] == f"ai-fix/{INCIDENT}-0"
		assert "+httpx==0.28.1" in request["proposed_fix"]["diff"]
		assert github.writes == []  # nothing touches GitHub before a human decides

		# A later report revision for the same attempt proposes nothing new.
		await service.on_report(event, report)
		assert len(bus.messages["approval.requested"]) == 1

		# 2. A named human approves through the approval API.
		decided = await http.post(
			f"/approvals/{INCIDENT}",
			json={"decision": "approved", "reviewer": "ishita", "comment": "0.99.0 does not exist"},
			headers={"Authorization": f"Bearer {TOKEN}"},
		)
		assert decided.status_code == 201, decided.text
		[decision] = bus.messages["approval.decided"]

		# 3. approval.decided -> PR policy -> branch + draft PR -> outcome reported back.
		final = await service.on_decision(decision)
		assert final.status == "pr_created"
		[pr] = github.pull_requests
		assert pr["draft"] is True and pr["head"] == f"ai-fix/{INCIDENT}-0"
		assert github.branches[pr["head"]][REQUIREMENTS].count("httpx==0.28.1") == 1
		stored = proposal(engine)
		assert (stored.status, stored.pull_request_url) == ("pr_created", final.pull_request_url)

		# A redelivered decision opens nothing more.
		await service.on_decision(decision)
		assert len(github.pull_requests) == 1

		# 4. Jenkins re-validates the ai-fix branch and it passes (TC-18).
		validated = await http.post(
			"/webhooks/jenkins/validation",
			json={"job_name": "target-app", "build_number": 43, "result": "SUCCESS", "branch": pr["head"]},
			headers={"X-Webhook-Token": WEBHOOK, "Content-Type": "application/json"},
		)
		assert validated.status_code == 202
	assert incident_status(engine) == "RESOLVED"
	assert proposal(engine).status == "validation_passed"


@pytest.mark.asyncio
async def test_rejected_fix_opens_nothing_and_closes_the_incident(stack, tmp_path: Path) -> None:
	"""TC-16 on the live path: no branch, no PR, the rejection on record."""
	engine, bus = stack
	github, pipeline, event, report = await investigate(tmp_path)
	async with backend_http() as http:
		service = service_for(pipeline, github, tmp_path, HttpApprovalBackend("http://agent-api", TOKEN, client=http))
		await service.on_report(event, report)

		await http.post(
			f"/approvals/{INCIDENT}",
			json={"decision": "rejected", "reviewer": "shreya", "comment": "I'll pick the version"},
			headers={"Authorization": f"Bearer {TOKEN}"},
		)
		final = await service.on_decision(bus.messages["approval.decided"][0])

	assert final.status == "rejected"
	assert github.writes == []
	assert proposal(engine).status == "rejected"
	assert incident_status(engine) == "CLOSED_REJECTED"
	with Session(engine) as session:
		assert session.execute(select(ApprovalRow.reviewer)).scalar_one() == "shreya"


@pytest.mark.asyncio
async def test_a_backend_outage_is_retried_without_reevaluating_policy(stack, tmp_path: Path) -> None:
	engine, bus = stack
	github, pipeline, event, report = await investigate(tmp_path)

	class FlakyBackend:
		def __init__(self, inner: HttpApprovalBackend) -> None:
			self.inner, self.calls = inner, 0

		async def register(self, payload: dict[str, Any]) -> None:
			self.calls += 1
			if self.calls == 1:
				raise BackendUnavailable("backend restarting")
			await self.inner.register(payload)

		async def report_outcome(self, incident_id: UUID, attempt: int, payload: dict[str, Any]) -> None:
			await self.inner.report_outcome(incident_id, attempt, payload)

	async with backend_http() as http:
		backend = FlakyBackend(HttpApprovalBackend("http://agent-api", TOKEN, client=http))
		service = service_for(pipeline, github, tmp_path, backend)

		await service.on_report(event, report)  # registration fails; the plan is kept
		assert "approval.requested" not in bus.messages
		await service.on_report(event, report)  # the next revision registers it

	assert backend.calls == 2
	assert len(bus.messages["approval.requested"]) == 1
	policy_runs = [r for r in await pipeline.audit.read(event.incident_id) if r.event_type == "policy_evaluated"]
	assert len(policy_runs) == 1  # gate 1 ran once, not once per attempt to register
