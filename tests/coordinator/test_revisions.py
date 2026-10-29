"""Report revisions, late results, audit repair and restart recovery.

**Owner:** P2 (Week 5). The invariants under test:

* one initial synthesis per ``(incident, attempt)``; a later evidence set makes a *revision*;
* the same evidence applied twice (duplicate message, recovery re-run) creates nothing;
* a result for an older attempt never replaces the current attempt's report;
* a revision persisted without its audit event is repaired by adding the event only;
* restarting the Coordinator any number of times never duplicates a revision or an event.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from common.audit import FileAuditLog
from common.evidence_store import FileEvidenceStore
from common.models import Evidence, FailureTaxonomy, IncidentCreatedEvent
from coordinator.agent import CoordinatorAgent
from coordinator.app import CoordinatorRuntime
from coordinator.revisions import (
    PersistResult,
    RevisionStore,
    evidence_fingerprint,
    revision_id,
)
from coordinator.synthesis import synthesize

pytestmark = pytest.mark.asyncio

DEP = FailureTaxonomy.DEPENDENCY_REGRESSION
UNKNOWN = FailureTaxonomy.UNKNOWN
AGENTS = ("jenkins_agent", "metrics_agent", "code_agent")


def make_event(attempt: int = 0) -> IncidentCreatedEvent:
    return IncidentCreatedEvent(
        incident_id=uuid4(),
        job_name="target-app",
        build_number=3,
        build_url="http://jenkins:8080/job/target-app/3/",
        branch="main",
        git_commit="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        timestamp=datetime.now(timezone.utc),
        remediation_attempt=attempt,
    )


def evidence(event, agent, failure_type=DEP, confidence=0.8, status="completed", created_at=None) -> Evidence:
    return Evidence(
        incident_id=event.incident_id,
        agent=agent,  # type: ignore[arg-type]
        created_at=created_at or datetime.now(timezone.utc),
        status=status,  # type: ignore[arg-type]
        failure_type=failure_type,
        summary=f"{agent} finding",
        confidence=confidence,
        redaction_applied=True,
    )


def ok(event: IncidentCreatedEvent, agent: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "incident_id": str(event.incident_id),
            "attempt": event.remediation_attempt,
            "blackboard_ref": {"section": "evidence", "key": agent, "version": 1},
        },
    )


class World:
    """A coordinator runtime over temp stores; ``restart()`` builds a fresh runtime on the same disk."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.evidence = FileEvidenceStore(tmp_path / "evidence")
        self.audit = FileAuditLog(tmp_path / "audit")
        self.revisions = RevisionStore(tmp_path / "reports")
        self.runtime = self.build()

    def build(self, a2a_agents=("jenkins_agent", "metrics_agent")) -> CoordinatorRuntime:
        runtime = CoordinatorRuntime()
        runtime.adk_agent = CoordinatorAgent(self.evidence)
        runtime.audit = self.audit
        runtime.revisions = self.revisions
        runtime.evidence_root = self.tmp / "evidence"
        runtime.a2a_agents = a2a_agents
        runtime.watch_timeout_seconds = 0.05
        runtime.watch_poll_seconds = 0.01
        runtime.late_window_seconds = 2.0
        runtime.late_poll_seconds = 0.02
        runtime.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: ok(self.event, "metrics_agent"))
        )
        return runtime

    event: IncidentCreatedEvent

    def restart(self) -> CoordinatorRuntime:
        self.runtime = self.build()
        return self.runtime

    async def audit_events(self, event, types=("synthesis_completed", "synthesis_revised")):
        return [r for r in await self.audit.read(event.incident_id) if r.event_type in types]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# ---- RevisionStore --------------------------------------------------------------------


def report_for(event, *agents):
    return synthesize([evidence(event, a) for a in agents], expected_agents=AGENTS)


async def test_the_same_evidence_is_persisted_once(tmp_path):
    store, event = RevisionStore(tmp_path), make_event()
    report = report_for(event, "jenkins_agent")
    first, rev = await store.persist(report, attempt=0, fingerprint="aaa", reason="initial")
    again, same = await store.persist(report, attempt=0, fingerprint="aaa", reason="initial")
    assert (first, again) == (PersistResult.CREATED, PersistResult.DUPLICATE)
    assert same.report_revision_id == rev.report_revision_id == revision_id(event.incident_id, 0, "aaa")
    assert len(await store.revisions(event.incident_id)) == 1


async def test_new_evidence_makes_a_numbered_revision(tmp_path):
    store, event = RevisionStore(tmp_path), make_event()
    await store.persist(report_for(event, "jenkins_agent"), attempt=0, fingerprint="aaa", reason="initial")
    result, second = await store.persist(
        report_for(event, "jenkins_agent", "code_agent"), attempt=0, fingerprint="bbb", reason="late_evidence"
    )
    assert result is PersistResult.CREATED and second.revision == 2
    assert [(r.revision, r.event_type) for r in await store.revisions(event.incident_id)] == [
        (1, "synthesis_completed"),
        (2, "synthesis_revised"),
    ]


async def test_an_older_attempt_never_replaces_the_current_one(tmp_path):
    store, event = RevisionStore(tmp_path), make_event()
    await store.persist(report_for(event, "jenkins_agent"), attempt=1, fingerprint="new", reason="initial")
    result, kept = await store.persist(report_for(event, "code_agent"), attempt=0, fingerprint="old", reason="late_evidence")
    assert result is PersistResult.STALE and kept.attempt == 1
    assert [r.attempt for r in await store.revisions(event.incident_id)] == [1]


async def test_a_new_attempt_starts_again_at_revision_one(tmp_path):
    store, event = RevisionStore(tmp_path), make_event()
    await store.persist(report_for(event, "jenkins_agent"), attempt=0, fingerprint="a0", reason="initial")
    result, revision = await store.persist(report_for(event, "jenkins_agent"), attempt=1, fingerprint="a1", reason="initial")
    assert result is PersistResult.CREATED and (revision.attempt, revision.revision) == (1, 1)


async def test_revisions_survive_a_new_store_instance(tmp_path):
    event = make_event()
    await RevisionStore(tmp_path).persist(report_for(event, "jenkins_agent"), attempt=0, fingerprint="aaa", reason="initial")
    reloaded = await RevisionStore(tmp_path).latest(event.incident_id)
    assert reloaded is not None and reloaded.report.incident_id == event.incident_id


async def test_the_fingerprint_changes_only_when_the_evidence_does():
    event = make_event()
    now = datetime.now(timezone.utc)
    a, b = evidence(event, "jenkins_agent", created_at=now), evidence(event, "code_agent", created_at=now)
    assert evidence_fingerprint([a, b]) == evidence_fingerprint([b, a])
    newer = evidence(event, "code_agent", created_at=now + timedelta(seconds=1))
    assert evidence_fingerprint([a, b]) != evidence_fingerprint([a, newer])


# ---- Runtime lifecycle ----------------------------------------------------------------


async def test_the_initial_synthesis_persists_one_revision_and_one_audit_event(world):
    event = world.event = make_event()
    for agent in AGENTS:
        await world.evidence.write(evidence(event, agent, DEP if agent != "metrics_agent" else UNKNOWN))
    try:
        outcomes = await world.runtime.dispatch_all(event)
    finally:
        await world.runtime.client.aclose()

    assert set(outcomes.values()) == {"completed"}
    [revision] = await world.revisions.revisions(event.incident_id)
    assert revision.revision == 1 and revision.report.complete
    [audited] = await world.audit_events(event)
    assert audited.event_type == "synthesis_completed"
    assert audited.payload["report_revision_id"] == revision.report_revision_id
    assert audited.payload["completeness"] == "complete" and audited.payload["reason"] == "initial"


async def test_a_redelivered_message_adds_no_revision_and_no_audit_event(world):
    event = world.event = make_event()
    for agent in AGENTS:
        await world.evidence.write(evidence(event, agent))
    try:
        await world.runtime.dispatch_all(event)
        await world.runtime.dispatch_all(event)
        fresh = world.restart()  # even a new runtime re-handling the same message
        await fresh.dispatch_all(event)
    finally:
        await world.runtime.client.aclose()
    assert len(await world.revisions.revisions(event.incident_id)) == 1
    assert len(await world.audit_events(event)) == 1


async def test_a_late_agent_produces_exactly_one_revision(world):
    event = world.event = make_event()
    await world.evidence.write(evidence(event, "jenkins_agent"))
    await world.evidence.write(evidence(event, "metrics_agent", UNKNOWN, 0.2))
    try:
        outcomes = await world.runtime.dispatch_all(event)  # code_agent never reports in time
        assert outcomes["code_agent"] == "timed_out"
        first = await world.revisions.latest(event.incident_id)
        assert first is not None and not first.report.complete and not first.report.actionable

        await world.evidence.write(evidence(event, "code_agent"))  # ...and now it does
        for _ in range(100):
            if len(await world.revisions.revisions(event.incident_id)) == 2:
                break
            await asyncio.sleep(0.05)
    finally:
        await world.runtime.stop()
        await world.runtime.client.aclose()

    revisions = await world.revisions.revisions(event.incident_id)
    assert [r.revision for r in revisions] == [1, 2]
    assert revisions[1].reason == "late_evidence" and revisions[1].report.complete
    assert [r.event_type for r in await world.audit_events(event)] == ["synthesis_completed", "synthesis_revised"]
    assert world.runtime.outcomes[str(event.incident_id)]["code_agent"] == "completed"


async def test_a_failed_document_arriving_late_does_not_create_a_revision(world):
    event = world.event = make_event()
    await world.evidence.write(evidence(event, "jenkins_agent"))
    await world.evidence.write(evidence(event, "metrics_agent", UNKNOWN, 0.2))
    try:
        await world.runtime.dispatch_all(event)
        await world.evidence.write(evidence(event, "code_agent", UNKNOWN, 0.0, status="failed"))
        await asyncio.sleep(0.3)
    finally:
        await world.runtime.stop()
        await world.runtime.client.aclose()
    assert len(await world.revisions.revisions(event.incident_id)) == 1


# ---- The crash-before-audit gap and restart recovery ----------------------------------


async def test_a_revision_without_its_audit_event_is_repaired_and_only_once(world):
    """Persist revision -> crash before the audit write -> restart -> repaired -> restart again."""
    event = world.event = make_event()
    for agent in AGENTS:
        await world.evidence.write(evidence(event, agent))
    view_fingerprint = evidence_fingerprint(await world.evidence.read_all(event.incident_id))
    report = synthesize(await world.evidence.read_all(event.incident_id))
    await world.revisions.persist(report, attempt=0, fingerprint=view_fingerprint, reason="initial")
    assert await world.audit_events(event) == []  # the crash: revision exists, audit event does not

    runtime = world.restart()
    try:
        assert await runtime.recover() == 1  # the audit event only
        [repaired] = await world.audit_events(event)
        assert repaired.payload["report_revision_id"] == revision_id(event.incident_id, 0, view_fingerprint)

        second = world.restart()
        assert await second.recover() == 0
        assert len(await world.revisions.revisions(event.incident_id)) == 1
        assert len(await world.audit_events(event)) == 1
    finally:
        await world.runtime.client.aclose()


async def test_recovery_synthesizes_an_incident_that_never_got_a_report_and_is_idempotent(world):
    event = world.event = make_event()
    await world.evidence.write(evidence(event, "jenkins_agent"))
    await world.evidence.write(evidence(event, "metrics_agent", UNKNOWN, 0.2))
    runtime = world.restart()
    try:
        assert await runtime.recover() == 1
        [revision] = await world.revisions.revisions(event.incident_id)
        assert revision.reason == "recovered"
        assert revision.report.agent_outcomes["code_agent"] == "timed_out"
        assert not revision.report.complete and not revision.report.actionable

        for _ in range(2):  # restart twice more: nothing new
            assert await world.restart().recover() == 0
        assert len(await world.revisions.revisions(event.incident_id)) == 1
        assert len(await world.audit_events(event)) == 1
    finally:
        await world.runtime.client.aclose()


async def test_recovery_skips_incidents_whose_evidence_is_old(world):
    event = world.event = make_event()
    await world.evidence.write(evidence(event, "jenkins_agent"))
    directory = world.tmp / "evidence" / str(event.incident_id)
    old = time.time() - 10 * 3600
    for path in directory.glob("*.json"):
        os.utime(path, (old, old))
    runtime = world.restart()
    try:
        assert await runtime.recover() == 0
    finally:
        await world.runtime.client.aclose()
    assert await world.revisions.revisions(event.incident_id) == []


async def test_recovery_after_a_late_agent_adds_one_revision_not_a_duplicate(world):
    event = world.event = make_event()
    await world.evidence.write(evidence(event, "jenkins_agent"))
    try:
        assert await world.restart().recover() == 1
        await world.evidence.write(evidence(event, "code_agent"))  # evidence moved on while we were down
        assert await world.restart().recover() == 1
        assert await world.restart().recover() == 0
    finally:
        await world.runtime.client.aclose()
    assert [r.revision for r in await world.revisions.revisions(event.incident_id)] == [1, 2]
    assert [r.event_type for r in await world.audit_events(event)] == ["synthesis_completed", "synthesis_revised"]


# ---- agent_dispatch audit events -------------------------------------------------------


async def test_every_dispatch_is_audited_with_its_outcome_and_a_duration(world):
    event = world.event = make_event()
    await world.evidence.write(evidence(event, "jenkins_agent"))
    await world.evidence.write(evidence(event, "metrics_agent", UNKNOWN, 0.2))
    try:
        await world.runtime.dispatch_all(event)  # code_agent never reports: timed_out
    finally:
        await world.runtime.stop()
        await world.runtime.client.aclose()

    dispatches = {r.payload["agent"]: r for r in await world.audit_events(event, types=("agent_dispatch",))}
    assert set(dispatches) == {"jenkins_agent", "metrics_agent", "code_agent"}
    assert dispatches["code_agent"].payload["outcome"] == "timed_out" and dispatches["code_agent"].ok is False
    assert dispatches["jenkins_agent"].payload["via"] == "a2a" and dispatches["jenkins_agent"].ok is True
    assert all(r.actor == "coordinator" and r.duration_ms is not None and r.duration_ms >= 0 for r in dispatches.values())


async def test_a_redelivered_message_adds_no_dispatch_events(world):
    event = world.event = make_event()
    for agent in AGENTS:
        await world.evidence.write(evidence(event, agent))
    try:
        await world.runtime.dispatch_all(event)
        await world.runtime.dispatch_all(event)
    finally:
        await world.runtime.client.aclose()
    assert len(await world.audit_events(event, types=("agent_dispatch",))) == 3


async def test_the_trail_a_real_dispatch_leaves_passes_the_audit_check_without_exemptions(world):
    from common.audit_check import check_incident, errors

    event = world.event = make_event()
    for agent in AGENTS:
        await world.evidence.write(evidence(event, agent))
    try:
        await world.runtime.dispatch_all(event)
    finally:
        await world.runtime.client.aclose()
    assert errors(check_incident(await world.audit.read(event.incident_id))) == []  # no unaudited_ok needed
