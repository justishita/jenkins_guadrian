"""The running Coordinator service fuses evidence after dispatch.

**Owner:** P3. ``tests/coordinator/test_dispatch.py`` covers the A2A dispatch itself
with no ADK agent attached; these cover what happens once one is: the service reads
the blackboard, fuses it, and keeps the report - and a fusion failure never turns a
completed dispatch into a failed one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from common.evidence_store import FileEvidenceStore
from common.models import Evidence, FailureTaxonomy, IncidentCreatedEvent
from coordinator.agent import CoordinatorAgent
from coordinator.app import CoordinatorRuntime


def make_event() -> IncidentCreatedEvent:
	return IncidentCreatedEvent(
		incident_id=uuid4(),
		job_name="target-app",
		build_number=17,
		build_url="http://jenkins:8080/job/target-app/17/",
		branch="main",
		git_commit="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
		timestamp=datetime.now(timezone.utc),
	)


def evidence(event: IncidentCreatedEvent, agent: str, failure_type: FailureTaxonomy, confidence: float) -> Evidence:
	return Evidence(
		incident_id=event.incident_id,
		agent=agent,  # type: ignore[arg-type]
		created_at=datetime.now(timezone.utc),
		status="completed",
		failure_type=failure_type,
		summary=f"{agent} finding",
		confidence=confidence,
		redaction_applied=True,
	)


def completed(event: IncidentCreatedEvent) -> Any:
	def handler(_: httpx.Request) -> httpx.Response:
		return httpx.Response(
			200,
			json={
				"status": "completed",
				"incident_id": str(event.incident_id),
				"attempt": event.remediation_attempt,
				"blackboard_ref": {"key": "jenkins_agent"},
			},
		)

	return handler


@pytest.mark.asyncio
async def test_dispatch_fuses_the_blackboard_into_a_report(tmp_path: Path) -> None:
	event = make_event()
	store = FileEvidenceStore(tmp_path / "evidence")
	await store.write(evidence(event, "jenkins_agent", FailureTaxonomy.DEPENDENCY_REGRESSION, 0.65))
	await store.write(evidence(event, "code_agent", FailureTaxonomy.DEPENDENCY_REGRESSION, 0.9))

	runtime = CoordinatorRuntime()
	runtime.adk_agent = CoordinatorAgent(store)
	runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(completed(event)))
	try:
		result = await runtime.dispatch_jenkins_task(event)
	finally:
		await runtime.client.aclose()

	assert result["status"] == "completed"
	report = runtime.reports[str(event.incident_id)]
	assert report.failure_type is FailureTaxonomy.DEPENDENCY_REGRESSION
	assert set(report.agreeing_agents) == {"code_agent", "jenkins_agent"}
	assert report.actionable is True


@pytest.mark.asyncio
async def test_the_dispatch_result_is_unchanged_by_fusion(tmp_path: Path) -> None:
	"""Callers of dispatch get the Jenkins agent's A2A response exactly as before."""
	event = make_event()
	store = FileEvidenceStore(tmp_path / "evidence")
	await store.write(evidence(event, "jenkins_agent", FailureTaxonomy.TIMEOUT, 0.85))

	runtime = CoordinatorRuntime()
	runtime.adk_agent = CoordinatorAgent(store)
	runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(completed(event)))
	try:
		result = await runtime.dispatch_jenkins_task(event)
	finally:
		await runtime.client.aclose()

	assert set(result) == {"status", "incident_id", "attempt", "blackboard_ref"}


@pytest.mark.asyncio
async def test_a_fusion_failure_does_not_fail_a_completed_dispatch(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	event = make_event()
	store = FileEvidenceStore(tmp_path / "evidence")
	await store.write(evidence(event, "jenkins_agent", FailureTaxonomy.TIMEOUT, 0.85))
	agent = CoordinatorAgent(store)

	async def broken(_: Any) -> None:
		raise RuntimeError("fusion exploded")

	monkeypatch.setattr(agent, "synthesize", broken)
	runtime = CoordinatorRuntime()
	runtime.adk_agent = agent
	runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(completed(event)))
	try:
		result = await runtime.dispatch_jenkins_task(event)
	finally:
		await runtime.client.aclose()

	assert result["status"] == "completed"
	assert str(event.incident_id) not in runtime.reports


@pytest.mark.asyncio
async def test_an_empty_blackboard_produces_no_report(tmp_path: Path) -> None:
	event = make_event()
	runtime = CoordinatorRuntime()
	runtime.adk_agent = CoordinatorAgent(FileEvidenceStore(tmp_path / "evidence"))
	runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(completed(event)))
	try:
		await runtime.dispatch_jenkins_task(event)
	finally:
		await runtime.client.aclose()

	assert runtime.reports == {}
