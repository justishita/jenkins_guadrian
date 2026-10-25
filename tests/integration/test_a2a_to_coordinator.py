"""All three agents announce to the ADK Coordinator over A2A.

**Owner:** P3. Each agent's own suite covers its investigation; this covers the seam
between them - that every agent emits a well-formed notification once its evidence is
on the blackboard, and that the Coordinator can turn those notifications into a view
of what every agent concluded.

These tests exercise the notifier for real. An earlier version of the Jenkins hook
referenced an undefined name and no test caught it, because every test left the
notifier unset.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from common.evidence_store import FileEvidenceStore
from common.models import Evidence, FailureTaxonomy, IncidentCreatedEvent
from common.a2a_contract import AGENT_NAMES, parse_evidence_ready
from coordinator.agent import CoordinatorAgent


INCIDENT = UUID("11111111-2222-3333-4444-555555555555")


class RecordingBus:
    """An in-process A2A sink that forwards to the Coordinator, like a real one would."""

    def __init__(self, coordinator: CoordinatorAgent) -> None:
        self._coordinator = coordinator
        self.messages: list[Any] = []

    async def __call__(self, message: Any) -> None:
        self.messages.append(message)
        await self._coordinator.on_evidence_ready(message)


def incident_event(**overrides: Any) -> IncidentCreatedEvent:
    payload: dict[str, Any] = {
        "incident_id": INCIDENT,
        "job_name": "target-app",
        "build_number": 17,
        "build_url": "http://jenkins:8080/job/target-app/17/",
        "branch": "main",
        "git_commit": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        "failed_stage": "Test",
        "timestamp": datetime(2026, 10, 25, 9, 31, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return IncidentCreatedEvent.model_validate(payload)


def evidence_for(agent: str, incident_id: UUID = INCIDENT) -> Evidence:
    return Evidence(
        incident_id=incident_id,
        agent=agent,  # type: ignore[arg-type]
        created_at=datetime.now(timezone.utc),
        status="completed",
        failure_type=FailureTaxonomy.DEPENDENCY_REGRESSION,
        summary=f"{agent} finding",
        confidence=0.9,
        redaction_applied=True,
    )


@pytest.fixture
def wiring(tmp_path: Path) -> tuple[FileEvidenceStore, CoordinatorAgent, RecordingBus]:
    store = FileEvidenceStore(tmp_path / "evidence")
    coordinator = CoordinatorAgent(store)
    return store, coordinator, RecordingBus(coordinator)


# --- the code agent, end to end -----------------------------------------------


@pytest.mark.asyncio
async def test_the_code_agent_announces_and_the_coordinator_sees_it(
    tmp_path: Path, wiring: tuple[FileEvidenceStore, CoordinatorAgent, RecordingBus]
) -> None:
    import httpx

    from agents.code_agent.agent import CodeInvestigationAgent
    from agents.code_agent.tools.github_client import GitHubClient
    from common.audit import FileAuditLog

    store, coordinator, bus = wiring
    sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    commit = {
        "sha": sha,
        "html_url": f"https://github.com/o/r/commit/{sha}",
        "commit": {"message": "Pin httpx", "author": {"name": "Ishita", "date": "2026-10-25T09:30:00Z"}},
        "files": [
            {
                "filename": "target_app/requirements.txt",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "patch": "@@ -4,1 +4,1 @@\n-httpx==0.28.1\n+httpx==0.99.0",
            }
        ],
    }
    routes = {
        f"/repos/o/r/commits/{sha}": commit,
        f"/repos/o/r/commits/{sha}/pulls": [],
        "/repos/o/r/commits": [commit],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=routes.get(request.url.path, []))

    github = GitHubClient(
        "o/r", "ghp_token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=0, backoff_seconds=0,
    )
    agent = CodeInvestigationAgent(
        github, store, FileAuditLog(tmp_path / "audit"), notifier=bus
    )

    await agent.run_event(incident_event())

    assert len(bus.messages) == 1
    incident_id, who = parse_evidence_ready(bus.messages[0])
    assert incident_id == INCIDENT
    assert who == "code_agent"
    assert (await coordinator.collect(INCIDENT)).reporting_agents == ("code_agent",)


# --- the announce hook on each agent ------------------------------------------


@pytest.mark.asyncio
async def test_the_jenkins_agent_announce_hook_works(
    wiring: tuple[FileEvidenceStore, CoordinatorAgent, RecordingBus]
) -> None:
    """Exercises the hook directly: an undefined name here once went unnoticed."""
    from agents.jenkins_agent.agent import JenkinsInvestigationAgent

    store, _, bus = wiring
    agent = JenkinsInvestigationAgent.__new__(JenkinsInvestigationAgent)
    agent.notifier = bus

    await agent._announce(evidence_for("jenkins_agent"))

    assert len(bus.messages) == 1
    assert parse_evidence_ready(bus.messages[0]) == (INCIDENT, "jenkins_agent")


@pytest.mark.asyncio
async def test_the_metrics_agent_announce_hook_works(
    wiring: tuple[FileEvidenceStore, CoordinatorAgent, RecordingBus]
) -> None:
    from agents.metrics_agent.consumer import MetricsAgent

    store, _, bus = wiring
    agent = MetricsAgent.__new__(MetricsAgent)
    agent._notifier = bus

    await agent._announce(evidence_for("metrics_agent"))

    assert len(bus.messages) == 1
    assert parse_evidence_ready(bus.messages[0]) == (INCIDENT, "metrics_agent")


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", ["jenkins_agent", "metrics_agent"])
async def test_a_failing_bus_does_not_break_an_agent(agent_name: str) -> None:
    """The evidence is already on the blackboard; a dropped notice must not undo it."""
    from agents.jenkins_agent.agent import JenkinsInvestigationAgent
    from agents.metrics_agent.consumer import MetricsAgent

    async def broken(_: Any) -> None:
        raise ConnectionError("coordinator unreachable")

    if agent_name == "jenkins_agent":
        agent = JenkinsInvestigationAgent.__new__(JenkinsInvestigationAgent)
        agent.notifier = broken
    else:
        agent = MetricsAgent.__new__(MetricsAgent)
        agent._notifier = broken

    await agent._announce(evidence_for(agent_name))  # must not raise


# --- the Coordinator's view ---------------------------------------------------


@pytest.mark.asyncio
async def test_all_three_agents_reporting_completes_the_blackboard(
    wiring: tuple[FileEvidenceStore, CoordinatorAgent, RecordingBus]
) -> None:
    store, coordinator, bus = wiring

    for agent in AGENT_NAMES:
        await store.write(evidence_for(agent))
        await bus(
            __import__(
                "common.a2a_contract", fromlist=["evidence_ready"]
            ).evidence_ready(INCIDENT, agent)
        )

    view = await coordinator.collect(INCIDENT)

    assert view.complete is True
    assert view.reporting_agents == tuple(sorted(AGENT_NAMES))
    assert len(bus.messages) == 3


@pytest.mark.asyncio
async def test_notifications_arriving_together_are_all_recorded(
    wiring: tuple[FileEvidenceStore, CoordinatorAgent, RecordingBus]
) -> None:
    """Agents run in parallel, so their notifications can land at the same moment."""
    from common.a2a_contract import evidence_ready

    store, coordinator, bus = wiring
    for agent in AGENT_NAMES:
        await store.write(evidence_for(agent))

    await asyncio.gather(*(bus(evidence_ready(INCIDENT, agent)) for agent in AGENT_NAMES))

    assert (await coordinator.collect(INCIDENT)).complete is True


@pytest.mark.asyncio
async def test_an_agent_announcing_a_different_incident_is_kept_apart(
    wiring: tuple[FileEvidenceStore, CoordinatorAgent, RecordingBus]
) -> None:
    from common.a2a_contract import evidence_ready

    store, coordinator, bus = wiring
    other = uuid4()
    await store.write(evidence_for("code_agent"))
    await store.write(evidence_for("metrics_agent", incident_id=other))

    await bus(evidence_ready(INCIDENT, "code_agent"))
    await bus(evidence_ready(other, "metrics_agent"))

    assert (await coordinator.collect(INCIDENT)).reporting_agents == ("code_agent",)
    assert (await coordinator.collect(other)).reporting_agents == ("metrics_agent",)
