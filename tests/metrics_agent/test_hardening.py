"""Metrics agent edge cases: deadline, mid-run outages, bad failure times, redelivery.

**Owner:** P2 (Week 5). Each case must end as evidence the Coordinator can reason about
(partial coverage, or ``failed`` with a reason), never as an exception or a silently
"normal" result for a query that never ran.
"""

import asyncio
from datetime import timedelta

import pytest

from agents.metrics_agent.consumer import MetricsAgent
from agents.metrics_agent.investigator import InvestigationConfig, Investigator
from agents.metrics_agent.models import IncidentCreatedEvent
from agents.metrics_agent.planner import CatalogPlanner
from agents.metrics_agent.prometheus_tool import PrometheusUnavailableError
from agents.metrics_agent.queries import CATALOG
from agents.metrics_agent.store import SharedStoreEvidenceWriter
from common.audit import FileAuditLog
from common.evidence_store import FileEvidenceStore
from tests.metrics_agent.helpers import FAILURE_TIME, NOW, FakeTool

NAMES = [spec.name for spec in CATALOG]


class Writer:
    def __init__(self) -> None:
        self.records = []

    def write(self, record) -> None:
        self.records.append(record)


def event(**overrides) -> IncidentCreatedEvent:
    fields = {
        "incident_id": "11111111-1111-4111-8111-111111111111",
        "job_name": "target-app",
        "build_number": 1,
        "branch": "main",
        "timestamp": FAILURE_TIME,
    }
    fields.update(overrides)
    return IncidentCreatedEvent.model_validate(fields)


def investigator(tool, writer=None, monotonic=None, **config) -> Investigator:
    return Investigator(
        tool,  # type: ignore[arg-type]
        CatalogPlanner(),
        writer or Writer(),
        InvestigationConfig(settle=timedelta(0), **config),
        clock=lambda: NOW,
        sleep=lambda _s: None,
        **({"monotonic": monotonic} if monotonic else {}),
    )


def down(*promqls: str) -> dict:
    return {promql: PrometheusUnavailableError("scrape target went away") for promql in promqls}


# ---- Prometheus flapping mid-investigation --------------------------------------------


def test_an_outage_after_some_queries_keeps_the_partial_evidence():
    # The first catalog query succeeds; every later one finds Prometheus down.
    tool = FakeTool(errors=down(*(spec.promql for spec in CATALOG[1:])))
    evidence = investigator(tool).investigate(event())

    assert evidence.status != "failed"
    assert evidence.investigation_coverage == pytest.approx(0.25)  # 1 of 4 queries measured
    assert evidence.agent == "metrics_agent"
    unusable = " ".join(evidence.coverage_notes)
    for name in NAMES[1:]:
        assert f"{name}: query error" in unusable
    # Only the query that failed was attempted; the rest were never sent.
    assert len(evidence.tool_calls) == 2 and [c.ok for c in evidence.tool_calls] == [True, False]


def test_an_outage_before_any_query_succeeds_is_still_a_failure():
    tool = FakeTool(errors=down(*(spec.promql for spec in CATALOG)))
    evidence = investigator(tool).investigate(event())
    assert evidence.status == "failed" and "Prometheus unavailable" in evidence.summary
    assert evidence.investigation_coverage is None


def test_queries_not_run_after_an_outage_are_not_reported_as_normal():
    tool = FakeTool(errors=down(*(spec.promql for spec in CATALOG[2:])))
    evidence = investigator(tool).investigate(event())
    metric_items = {item.id: item.content for item in evidence.evidence_items}
    for name in NAMES[2:]:
        assert '"outcome": "error"' in metric_items[f"metric-{name}"]


# ---- Wall-clock deadline --------------------------------------------------------------


class Ticker:
    """A monotonic clock that advances ``step`` seconds every time it is read."""

    def __init__(self, step: float) -> None:
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def test_the_deadline_stops_further_queries_and_lowers_coverage():
    tool = FakeTool()
    # Reads: 1 for the budget start, then 1 per query. 40 s per read with a 90 s budget:
    # queries start at t=40, 80 (inside) and 120, 160 (outside).
    evidence = investigator(tool, monotonic=Ticker(40.0), deadline=timedelta(seconds=90)).investigate(event())

    assert len(tool.calls) == 2
    assert evidence.status != "failed"
    assert evidence.investigation_coverage == pytest.approx(0.5)
    assert sum("deadline exceeded" in note for note in evidence.coverage_notes) == 2


def test_a_generous_deadline_changes_nothing():
    tool = FakeTool()
    evidence = investigator(tool, monotonic=Ticker(0.001)).investigate(event())
    assert len(tool.calls) == 4 and evidence.investigation_coverage == pytest.approx(1.0)


# ---- Failure time edge cases ----------------------------------------------------------


def test_a_failure_time_in_the_future_is_a_failed_investigation_not_a_crash():
    evidence = investigator(FakeTool()).investigate(event(timestamp=NOW + timedelta(days=2)))
    assert evidence.status == "failed" and "Invalid investigation window" in evidence.summary


def test_a_failure_time_long_ago_has_no_data_and_says_so():
    tool = FakeTool(overrides={spec.promql: None for spec in CATALOG})
    evidence = investigator(tool).investigate(event(timestamp=NOW - timedelta(days=30)))
    assert evidence.status == "insufficient_evidence"
    assert evidence.investigation_coverage == 0.0 and evidence.failure_type.value == "unknown"


def test_many_series_for_one_query_do_not_break_the_investigation():
    # Prometheus can return a series per label set; the sanity step keeps the most populated one.
    evidence = investigator(FakeTool()).investigate(event())
    assert evidence.status in ("completed", "insufficient_evidence")


# ---- Redelivery -----------------------------------------------------------------------


def test_delivering_the_same_incident_twice_versions_the_evidence_instead_of_duplicating_it(tmp_path):
    store = FileEvidenceStore(tmp_path / "evidence")
    agent = MetricsAgent(
        Investigator(
            FakeTool(),  # type: ignore[arg-type]
            CatalogPlanner(),
            SharedStoreEvidenceWriter(store),
            InvestigationConfig(settle=timedelta(0)),
            clock=lambda: NOW,
            sleep=lambda _s: None,
        ),
        FileAuditLog(tmp_path / "audit"),
    )
    incident = event()

    async def run() -> None:
        await agent.run_event(incident)
        await agent.run_event(incident)

    asyncio.run(run())

    path = tmp_path / "evidence" / str(incident.incident_id)
    assert [p.name for p in path.glob("*.json")] == ["metrics_agent.json"]  # one document...
    assert '"version": 2' in (path / "metrics_agent.json").read_text(encoding="utf-8")  # ...bumped, not duplicated
    audit = asyncio.run(FileAuditLog(tmp_path / "audit").read(incident.incident_id))
    # Each delivery is its own audited run: a start and a terminal event per run.
    assert [r.event_type for r in audit if r.event_type in ("agent_started", "agent_completed")].count("agent_started") == 2
