import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agents.metrics_agent.evidence import EvidenceRecord
from agents.metrics_agent.investigator import InvestigationConfig, Investigator
from agents.metrics_agent.models import IncidentCreatedEvent, QueryResult
from agents.metrics_agent.planner import CatalogPlanner
from agents.metrics_agent.prometheus_tool import (
    PrometheusQueryError,
    PrometheusUnavailableError,
)
from agents.metrics_agent.queries import (
    AVAILABILITY,
    CATALOG,
    CPU_RATE,
    LATENCY_P95,
    MEMORY_RSS,
    QuerySpec,
)
from agents.metrics_agent.store import LocalJsonEvidenceWriter
from agents.metrics_agent.taxonomy import FailureTaxonomy
from tests.metrics_agent.helpers import (
    FAILURE_TIME,
    NOW,
    flat,
    make_window,
    samples,
    step_up,
)

MIB = 1024 * 1024
Fn = Callable[[datetime], float]


class FakeTool:
    """Stands in for PrometheusTool; serves a synthetic series per catalog query."""

    def __init__(self, overrides: dict[str, Fn] | None = None, errors: dict[str, Exception] | None = None) -> None:
        window = make_window()
        self.defaults: dict[str, Fn] = {
            AVAILABILITY.promql: flat(1.0),
            LATENCY_P95.promql: flat(0.007),
            CPU_RATE.promql: flat(0.002),
            MEMORY_RSS.promql: flat(80 * MIB),
        }
        self.defaults.update(overrides or {})
        self.errors = errors or {}
        self.window = window
        self.calls: list[str] = []

    def query_range(self, promql: str, start: datetime, end: datetime, step: str) -> QueryResult:
        self.calls.append(promql)
        if promql in self.errors:
            raise self.errors[promql]
        fn = self.defaults[promql]
        if fn is None:  # type: ignore[comparison-overlap]
            return QueryResult(promql=promql, result_type="matrix", series=[])
        from agents.metrics_agent.models import Series

        return QueryResult(
            promql=promql,
            result_type="matrix",
            series=[Series(labels={}, samples=samples(start, end, fn, inclusive_end=True))],
        )


class MemoryWriter:
    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []

    def write(self, record: EvidenceRecord) -> None:
        self.records.append(record)


def event(**overrides: object) -> IncidentCreatedEvent:
    data: dict[str, object] = {"incident_id": str(uuid4()), "timestamp": FAILURE_TIME, "failed_stage": "Test"}
    data.update(overrides)
    return IncidentCreatedEvent.model_validate(data)


def run(tool: FakeTool, ev: IncidentCreatedEvent | None = None) -> tuple[EvidenceRecord, MemoryWriter]:
    writer = MemoryWriter()
    investigator = Investigator(
        tool,  # type: ignore[arg-type]
        CatalogPlanner(),
        writer,
        InvestigationConfig(),
        clock=lambda: NOW,
        sleep=lambda _seconds: None,  # never really wait in tests
    )
    return investigator.investigate(ev or event()), writer


class TickingClock:
    """A clock that `sleep` advances, so settle behaviour is testable without waiting."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def test_recent_failure_waits_for_metrics_to_settle_then_queries() -> None:
    clock = TickingClock(FAILURE_TIME + timedelta(seconds=5))
    writer = MemoryWriter()
    investigator = Investigator(
        FakeTool(),  # type: ignore[arg-type]
        CatalogPlanner(),
        writer,
        InvestigationConfig(settle=timedelta(seconds=30)),
        clock=clock,
        sleep=clock.sleep,
    )
    record = investigator.investigate(event())
    assert clock.slept == [25.0]
    assert record.status == "insufficient_evidence"


def test_old_failure_does_not_wait() -> None:
    clock = TickingClock(FAILURE_TIME + timedelta(minutes=10))
    investigator = Investigator(
        FakeTool(),  # type: ignore[arg-type]
        CatalogPlanner(),
        MemoryWriter(),
        clock=clock,
        sleep=clock.sleep,
    )
    investigator.investigate(event())
    assert clock.slept == []


def test_settle_wait_is_capped_even_for_a_far_future_timestamp() -> None:
    clock = TickingClock(NOW)
    investigator = Investigator(
        FakeTool(),  # type: ignore[arg-type]
        CatalogPlanner(),
        MemoryWriter(),
        InvestigationConfig(settle=timedelta(seconds=30)),
        clock=clock,
        sleep=clock.sleep,
    )
    investigator.investigate(event(timestamp=NOW + timedelta(hours=3)))
    assert clock.slept == [30.0]


def test_latency_fault_classified_as_timeout() -> None:
    window = make_window()
    record, writer = run(FakeTool({LATENCY_P95.promql: step_up(window, 0.007, 2.0)}))
    assert record.status == "completed"
    assert record.failure_type == FailureTaxonomy.TIMEOUT
    assert record.root_cause_hypotheses[0].failure_type == FailureTaxonomy.TIMEOUT
    assert 0.6 < record.confidence <= 0.95
    assert [i.id for i in record.evidence_items] == [f"metric-{s.name}" for s in CATALOG]
    assert len(record.tool_calls) == len(CATALOG) and all(c.ok for c in record.tool_calls)
    assert writer.records == [record]


def test_memory_leak_classified_as_resource_exhaustion() -> None:
    window = make_window()
    record, _ = run(FakeTool({MEMORY_RSS.promql: step_up(window, 80 * MIB, 100 * MIB)}))
    assert record.failure_type == FailureTaxonomy.RESOURCE_EXHAUSTION
    assert record.status == "completed"


def test_strongest_anomaly_ranks_first_when_several_fire() -> None:
    window = make_window()
    record, _ = run(
        FakeTool(
            {
                LATENCY_P95.promql: step_up(window, 0.007, 2.0),
                CPU_RATE.promql: step_up(window, 0.002, 0.05),
            }
        )
    )
    confidences = [h.confidence for h in record.root_cause_hypotheses]
    assert len(confidences) == 2 and confidences == sorted(confidences, reverse=True)
    assert record.confidence == confidences[0]


def test_healthy_system_reports_insufficient_evidence_not_a_false_positive() -> None:
    record, _ = run(FakeTool())
    assert record.status == "insufficient_evidence"
    assert record.failure_type == FailureTaxonomy.UNKNOWN
    assert record.confidence == pytest.approx(0.2)
    assert all('"outcome": "normal"' in i.content for i in record.evidence_items)


def test_target_down_points_to_infra_failure_even_when_other_metrics_vanish() -> None:
    window = make_window()
    tool = FakeTool(
        {
            AVAILABILITY.promql: step_up(window, 1.0, 0.0),
            LATENCY_P95.promql: None,  # type: ignore[dict-item]
            CPU_RATE.promql: None,  # type: ignore[dict-item]
            MEMORY_RSS.promql: None,  # type: ignore[dict-item]
        }
    )
    record, _ = run(tool)
    assert record.failure_type == FailureTaxonomy.INFRA_NETWORK_FAILURE
    unusable = [i for i in record.evidence_items if '"outcome": "unusable"' in i.content]
    assert len(unusable) == 3


def test_no_usable_data_is_zero_confidence_unknown() -> None:
    tool = FakeTool({s.promql: None for s in CATALOG})  # type: ignore[dict-item]
    record, _ = run(tool)
    assert record.status == "insufficient_evidence" and record.confidence == 0.0


def test_prometheus_unavailable_yields_failed_evidence_without_raising() -> None:
    tool = FakeTool(errors={AVAILABILITY.promql: PrometheusUnavailableError("down")})
    record, writer = run(tool)
    assert record.status == "failed" and record.confidence == 0.0
    assert record.failure_type == FailureTaxonomy.UNKNOWN
    assert "unavailable" in record.summary
    assert writer.records == [record]
    assert tool.calls == [AVAILABILITY.promql]  # stops hammering a dead Prometheus


def test_one_bad_query_does_not_sink_the_investigation() -> None:
    window = make_window()
    tool = FakeTool(
        {MEMORY_RSS.promql: step_up(window, 80 * MIB, 100 * MIB)},
        errors={LATENCY_P95.promql: PrometheusQueryError("bad_data: parse error")},
    )
    record, _ = run(tool)
    assert record.failure_type == FailureTaxonomy.RESOURCE_EXHAUSTION
    errored = [i for i in record.evidence_items if '"outcome": "error"' in i.content]
    assert len(errored) == 1 and sum(not c.ok for c in record.tool_calls) == 1


def test_unexpected_crash_becomes_failed_evidence() -> None:
    tool = FakeTool(errors={AVAILABILITY.promql: RuntimeError("boom")})
    record, writer = run(tool)
    assert record.status == "failed" and "Internal error" in record.summary
    assert "boom" not in record.summary  # internals are not leaked into evidence
    assert len(writer.records) == 1


def test_missing_failure_timestamp_falls_back_to_received_at_then_now() -> None:
    record, _ = run(FakeTool(), event(timestamp=None, received_at=FAILURE_TIME))
    assert record.status == "insufficient_evidence"
    record, _ = run(FakeTool(), event(timestamp=None))
    assert record.status in {"insufficient_evidence", "completed"}  # window ends at "now", still valid


def test_failure_far_in_future_is_failed_evidence() -> None:

    record, _ = run(FakeTool(), event(timestamp=NOW + timedelta(hours=3)))
    assert record.status == "failed" and "window" in record.summary


def test_evidence_items_carry_promql_window_and_detection() -> None:
    window = make_window()
    record, _ = run(FakeTool({CPU_RATE.promql: step_up(window, 0.002, 0.9)}))
    item = next(i for i in record.evidence_items if i.id == "metric-cpu_rate")
    content = json.loads(item.content)
    assert content["promql"] == CPU_RATE.promql
    assert content["outcome"] == "anomalous"
    assert content["detection"]["anomalous"] is True
    assert set(content["window"]) == {"baseline_start", "start", "end"}


def test_records_validate_against_shared_schema_for_every_outcome(tmp_path: Path) -> None:
    window = make_window()
    scenarios: list[FakeTool] = [
        FakeTool({LATENCY_P95.promql: step_up(window, 0.007, 2.0)}),
        FakeTool(),
        FakeTool({s.promql: None for s in CATALOG}),  # type: ignore[dict-item]
        FakeTool(errors={AVAILABILITY.promql: PrometheusUnavailableError("down")}),
    ]
    for tool in scenarios:
        investigator = Investigator(
            tool,  # type: ignore[arg-type]
            CatalogPlanner(),
            LocalJsonEvidenceWriter(tmp_path),
            InvestigationConfig(),
            clock=lambda: NOW,
            sleep=lambda _seconds: None,
        )
        investigator.investigate(event())
    assert len(list(tmp_path.glob("*.metrics_agent.json"))) == len(scenarios)


def test_deploy_failure_checks_availability_first() -> None:
    specs: list[QuerySpec] = CatalogPlanner().plan(event(failed_stage="Deploy"))
    assert specs[0] is AVAILABILITY
    assert [s.name for s in CatalogPlanner().plan(event(failed_stage="Test"))] == [s.name for s in CATALOG]
