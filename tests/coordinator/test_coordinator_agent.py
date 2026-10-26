"""Tests for the ADK Coordinator agent.

**Owner:** P3. The Coordinator is the only component allowed to see all three evidence
documents. ``collect`` must return the raw, unfused view - exactly what each agent
said - and ``synthesize`` is the separate step that fuses it. Keeping them apart means
a reviewer can always see the agents' own words before the Coordinator's verdict.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from common.evidence_store import FileEvidenceStore
from common.models import Evidence, FailureTaxonomy
from common.a2a_contract import A2AContractError, evidence_ready
from coordinator.agent import COORDINATOR_NAME, BlackboardView, CoordinatorAgent


INCIDENT = UUID("11111111-2222-3333-4444-555555555555")


def evidence_for(
	agent: str,
	*,
	failure_type: FailureTaxonomy = FailureTaxonomy.DEPENDENCY_REGRESSION,
	confidence: float = 0.9,
	status: str = "completed",
	incident_id: UUID = INCIDENT,
) -> Evidence:
	return Evidence(
		incident_id=incident_id,
		agent=agent,  # type: ignore[arg-type]
		created_at=datetime.now(timezone.utc),
		status=status,  # type: ignore[arg-type]
		failure_type=failure_type,
		summary=f"{agent} finding",
		confidence=confidence,
		redaction_applied=True,
	)


@pytest.fixture
def store(tmp_path: Path) -> FileEvidenceStore:
	return FileEvidenceStore(tmp_path / "evidence")


@pytest.fixture
def coordinator(store: FileEvidenceStore) -> CoordinatorAgent:
	return CoordinatorAgent(store)


def test_the_coordinator_is_an_adk_agent(coordinator: CoordinatorAgent) -> None:
	from google.adk.agents import BaseAgent

	assert isinstance(coordinator, BaseAgent)
	assert coordinator.name == COORDINATOR_NAME


@pytest.mark.asyncio
async def test_an_empty_blackboard_reports_nothing_rather_than_failing(
	coordinator: CoordinatorAgent,
) -> None:
	view = await coordinator.collect(INCIDENT)

	assert view.evidence == ()
	assert view.complete is False
	assert "No agent has written evidence" in view.summary()


@pytest.mark.asyncio
async def test_it_reads_every_agent_off_the_blackboard(
	coordinator: CoordinatorAgent, store: FileEvidenceStore
) -> None:
	for agent in ("jenkins_agent", "metrics_agent", "code_agent"):
		await store.write(evidence_for(agent))

	view = await coordinator.collect(INCIDENT)

	assert view.reporting_agents == ("code_agent", "jenkins_agent", "metrics_agent")
	assert view.complete is True
	assert view.missing_agents == ()


@pytest.mark.asyncio
async def test_partial_reporting_names_who_is_missing(
	coordinator: CoordinatorAgent, store: FileEvidenceStore
) -> None:
	await store.write(evidence_for("code_agent"))

	view = await coordinator.collect(INCIDENT)

	assert view.complete is False
	assert view.missing_agents == ("jenkins_agent", "metrics_agent")
	assert "still waiting on jenkins_agent, metrics_agent" in view.summary()


@pytest.mark.asyncio
async def test_collect_returns_the_raw_unfused_view(
	coordinator: CoordinatorAgent, store: FileEvidenceStore
) -> None:
	"""`collect` is the unfused view; fusion lives only in `synthesize`."""
	await store.write(evidence_for("code_agent", failure_type=FailureTaxonomy.CONFIG_ERROR, confidence=0.9))
	await store.write(
		evidence_for("metrics_agent", failure_type=FailureTaxonomy.TIMEOUT, confidence=0.4)
	)

	view = await coordinator.collect(INCIDENT)
	by_agent = {item.agent: item for item in view.evidence}

	assert by_agent["code_agent"].failure_type is FailureTaxonomy.CONFIG_ERROR
	assert by_agent["metrics_agent"].failure_type is FailureTaxonomy.TIMEOUT
	assert by_agent["code_agent"].confidence == pytest.approx(0.9)
	# No combined verdict exists anywhere on the view.
	assert not hasattr(view, "failure_type")
	assert not hasattr(view, "confidence")


@pytest.mark.asyncio
async def test_the_summary_reports_each_agent_separately(
	coordinator: CoordinatorAgent, store: FileEvidenceStore
) -> None:
	await store.write(evidence_for("code_agent", failure_type=FailureTaxonomy.CONFIG_ERROR, confidence=0.9))
	await store.write(evidence_for("metrics_agent", failure_type=FailureTaxonomy.UNKNOWN, confidence=0.2))

	summary = (await coordinator.collect(INCIDENT)).summary()

	assert "code_agent: config_error at 0.90" in summary
	assert "metrics_agent: unknown at 0.20" in summary


@pytest.mark.asyncio
async def test_an_a2a_notification_triggers_a_blackboard_read(
	coordinator: CoordinatorAgent, store: FileEvidenceStore
) -> None:
	await store.write(evidence_for("code_agent"))

	view = await coordinator.on_evidence_ready(evidence_ready(INCIDENT, "code_agent"))

	assert view.incident_id == INCIDENT
	assert view.reporting_agents == ("code_agent",)


@pytest.mark.asyncio
async def test_a_notification_is_believed_only_about_timing_not_content(
	coordinator: CoordinatorAgent, store: FileEvidenceStore
) -> None:
	"""An agent can announce itself without having written; the store is the truth."""
	view = await coordinator.on_evidence_ready(evidence_ready(INCIDENT, "code_agent"))

	assert view.evidence == ()


@pytest.mark.asyncio
async def test_a_malformed_notification_is_rejected(coordinator: CoordinatorAgent) -> None:
	message = evidence_ready(INCIDENT, "code_agent")
	message.metadata["kind"] = "something.else"

	with pytest.raises(A2AContractError):
		await coordinator.on_evidence_ready(message)


@pytest.mark.asyncio
async def test_repeated_notifications_from_one_agent_are_not_double_counted(
	coordinator: CoordinatorAgent, store: FileEvidenceStore
) -> None:
	await store.write(evidence_for("code_agent"))
	for _ in range(3):
		view = await coordinator.on_evidence_ready(evidence_ready(INCIDENT, "code_agent"))

	assert view.reporting_agents == ("code_agent",)


@pytest.mark.asyncio
async def test_incidents_are_kept_apart(
	coordinator: CoordinatorAgent, store: FileEvidenceStore
) -> None:
	other = uuid4()
	await store.write(evidence_for("code_agent"))
	await store.write(evidence_for("metrics_agent", incident_id=other))

	assert (await coordinator.collect(INCIDENT)).reporting_agents == ("code_agent",)
	assert (await coordinator.collect(other)).reporting_agents == ("metrics_agent",)


def test_a_view_of_nothing_is_not_complete() -> None:
	assert BlackboardView(incident_id=INCIDENT).complete is False



@pytest.mark.asyncio
async def test_synthesize_fuses_the_blackboard_into_one_report(
	coordinator: CoordinatorAgent, store: FileEvidenceStore
) -> None:
	await store.write(evidence_for("code_agent", failure_type=FailureTaxonomy.DEPENDENCY_REGRESSION, confidence=0.8))
	await store.write(evidence_for("jenkins_agent", failure_type=FailureTaxonomy.DEPENDENCY_REGRESSION, confidence=0.7))
	await store.write(
		evidence_for("metrics_agent", failure_type=FailureTaxonomy.UNKNOWN, confidence=0.2, status="insufficient_evidence")
	)

	report = await coordinator.synthesize(INCIDENT)

	assert report is not None
	assert report.failure_type is FailureTaxonomy.DEPENDENCY_REGRESSION
	assert report.agreeing_agents == ("code_agent", "jenkins_agent")
	assert report.complete and report.actionable is True


@pytest.mark.asyncio
async def test_synthesize_on_an_empty_blackboard_returns_none(coordinator: CoordinatorAgent) -> None:
	assert await coordinator.synthesize(INCIDENT) is None


@pytest.mark.asyncio
async def test_synthesize_does_not_wait_for_every_agent(
	coordinator: CoordinatorAgent, store: FileEvidenceStore
) -> None:
	await store.write(evidence_for("code_agent", confidence=0.9))

	report = await coordinator.synthesize(INCIDENT)

	assert report is not None
	assert report.reporting_agents == ("code_agent",)
