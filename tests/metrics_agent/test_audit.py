import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agents.metrics_agent.consumer import MetricsAgent
from agents.metrics_agent.investigator import InvestigationConfig, Investigator
from agents.metrics_agent.models import IncidentCreatedEvent
from agents.metrics_agent.planner import CatalogPlanner
from agents.metrics_agent.prometheus_tool import PrometheusUnavailableError
from agents.metrics_agent.queries import AVAILABILITY, LATENCY_P95
from agents.metrics_agent.store import SharedStoreEvidenceWriter
from common.audit import AuditLog, AuditRecord, BestEffortAuditLog, FileAuditLog
from common.evidence_store import FileEvidenceStore
from common.models import Evidence
from tests.metrics_agent.helpers import (
    FAILURE_TIME,
    NOW,
    FakeTool,
    make_window,
    step_up,
)


def event() -> IncidentCreatedEvent:
    return IncidentCreatedEvent.model_validate(
        {"incident_id": str(uuid4()), "timestamp": FAILURE_TIME, "failed_stage": "Test"}
    )


def build(tmp_path: Path, tool: FakeTool, audit: AuditLog | None = None, writer: object | None = None) -> MetricsAgent:
    store = FileEvidenceStore(tmp_path / "evidence")
    investigator = Investigator(
        tool,  # type: ignore[arg-type]
        CatalogPlanner(),
        writer or SharedStoreEvidenceWriter(store),  # type: ignore[arg-type]
        InvestigationConfig(settle=timedelta(0)),
        clock=lambda: NOW,
        sleep=lambda _seconds: None,
    )
    return MetricsAgent(investigator, audit if audit is not None else FileAuditLog(tmp_path / "audit"))


def trail(tmp_path: Path, incident_id: UUID) -> list[AuditRecord]:
    return asyncio.run(FileAuditLog(tmp_path / "audit").read(incident_id))


def test_successful_investigation_leaves_an_ordered_audit_trail(tmp_path: Path) -> None:
    ev = event()
    tool = FakeTool({LATENCY_P95.promql: step_up(make_window(), 0.007, 2.0)})
    evidence = asyncio.run(build(tmp_path, tool).run_event(ev))

    records = trail(tmp_path, ev.incident_id)
    types = [r.event_type for r in records]
    assert types[0] == "agent_started" and types[-1] == "agent_completed"
    assert types[-3:-1] == ["hypothesis_formed", "evidence_written"]
    assert all(r.actor == "metrics_agent" for r in records)
    assert all(r.ok is not False for r in records)

    # One tool_call per query the investigation actually made - whatever the plan was.
    calls = [r for r in records if r.event_type == "tool_call"]
    assert len(calls) == len(evidence.tool_calls) == len(tool.calls)
    assert [c.payload["args"]["promql"] for c in calls] == tool.calls

    completed = records[-1]
    assert completed.payload["status"] == "completed"
    assert completed.payload["failure_type"] == "timeout"
    assert completed.payload["tool_calls"] == len(calls)
    assert completed.duration_ms is not None


def test_audit_agrees_with_the_stored_evidence(tmp_path: Path) -> None:
    ev = event()
    evidence = asyncio.run(build(tmp_path, FakeTool()).run_event(ev))
    stored = asyncio.run(FileEvidenceStore(tmp_path / "evidence").read(ev.incident_id, "metrics_agent"))
    assert stored == evidence
    written = next(r for r in trail(tmp_path, ev.incident_id) if r.event_type == "evidence_written")
    assert written.payload["confidence"] == stored.confidence  # type: ignore[union-attr]
    assert written.payload["evidence_items"] == len(stored.evidence_items)  # type: ignore[union-attr]


def test_prometheus_outage_is_audited_as_a_failed_call_but_a_completed_run(tmp_path: Path) -> None:
    ev = event()
    tool = FakeTool(errors={AVAILABILITY.promql: PrometheusUnavailableError("down")})
    asyncio.run(build(tmp_path, tool).run_event(ev))

    records = trail(tmp_path, ev.incident_id)
    calls = [r for r in records if r.event_type == "tool_call"]
    assert [c.ok for c in calls] == [False]
    completed = records[-1]
    assert completed.event_type == "agent_completed" and completed.payload["status"] == "failed"


def test_evidence_store_failure_is_audited_as_agent_failed_and_reraised(tmp_path: Path) -> None:
    class BrokenWriter:
        def write(self, record: Evidence) -> None:
            raise OSError("disk full")

    ev = event()
    with pytest.raises(OSError):
        asyncio.run(build(tmp_path, FakeTool(), writer=BrokenWriter()).run_event(ev))

    records = trail(tmp_path, ev.incident_id)
    assert [r.event_type for r in records] == ["agent_started", "agent_failed"]
    assert records[-1].ok is False and "OSError" in records[-1].summary


def test_a_broken_audit_log_never_blocks_the_investigation(tmp_path: Path) -> None:
    class BrokenAudit(AuditLog):
        async def record(self, record: AuditRecord) -> None:
            raise ConnectionError("audit db down")

        async def read(self, incident_id: UUID | str) -> list[AuditRecord]:
            return []

    ev = event()
    agent = build(tmp_path, FakeTool(), audit=BestEffortAuditLog(BrokenAudit()))
    evidence = asyncio.run(agent.run_event(ev))
    assert evidence.status == "insufficient_evidence"
    assert asyncio.run(FileEvidenceStore(tmp_path / "evidence").read(ev.incident_id, "metrics_agent")) is not None


def test_without_an_audit_log_the_agent_still_investigates(tmp_path: Path) -> None:
    store = FileEvidenceStore(tmp_path / "evidence")
    investigator = Investigator(
        FakeTool(),  # type: ignore[arg-type]
        CatalogPlanner(),
        SharedStoreEvidenceWriter(store),
        InvestigationConfig(settle=timedelta(0)),
        clock=lambda: NOW,
        sleep=lambda _seconds: None,
    )
    ev = event()
    evidence = asyncio.run(MetricsAgent(investigator).run_event(ev))
    assert evidence.agent == "metrics_agent"
    assert not (tmp_path / "audit").exists()
