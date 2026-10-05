"""Turns a `scenarios/*.yaml` file with `agent: metrics_agent` into a real investigation.

A scenario describes, in data, what Prometheus would have shown (`metrics:`), what the
incident looked like (`incident:`), and what the agent must conclude (`expected:`). This
module builds a fake Prometheus from the first, runs the real agent end to end (Investigator,
shared Evidence Store, audit trail), and hands the result back for checking.

`metrics:` keys are catalog metric names (`target_availability`, `latency_p95`, `cpu_rate`,
`memory_rss`); a metric that is not listed behaves normally. Times are seconds relative to
the failure time (negative = before it).

    metrics:
      latency_p95:
        baseline: 0.007          # normal level (optional; sensible default per metric)
        noise: 0.0005            # optional +/- wobble around the baseline
        fault:                   # optional
          shape: step            # step | ramp | spike | missing | stops
          at: -120               # when the fault starts
          level: 2.0             # step / ramp / spike level
          until: 0               # ramp: when the level is reached
          duration: 60           # spike: how long it lasts
    prometheus:
      unavailable: true          # every query fails as if Prometheus were down
"""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from agents.metrics_agent.consumer import MetricsAgent
from agents.metrics_agent.investigator import InvestigationConfig, Investigator
from agents.metrics_agent.models import IncidentCreatedEvent, QueryResult, Series
from agents.metrics_agent.planner import CatalogPlanner
from agents.metrics_agent.prometheus_tool import PrometheusUnavailableError
from agents.metrics_agent.queries import CATALOG
from agents.metrics_agent.store import SharedStoreEvidenceWriter
from common.audit import AuditRecord, FileAuditLog
from common.evidence_store import FileEvidenceStore
from common.models import Evidence
from tests.metrics_agent.helpers import FAILURE_TIME, MIB, NOW, FakeTool

SCENARIO_DIR = Path(__file__).resolve().parents[2] / "scenarios"
AGENT = "metrics_agent"

DEFAULT_BASELINES = {
    "target_availability": 1.0,
    "latency_p95": 0.007,
    "cpu_rate": 0.002,
    "memory_rss": 80 * MIB,
}
_PROMQL_BY_NAME = {spec.name: spec.promql for spec in CATALOG}


def load_metrics_scenarios() -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for path in sorted(SCENARIO_DIR.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{path.name} is not a mapping"
        if document.get("agent") == AGENT:
            document["_file"] = path.name
            scenarios.append(document)
    return scenarios


def scenario_id(scenario: dict[str, Any]) -> str:
    return str(scenario["id"])


def _series_fn(name: str, config: dict[str, Any]) -> Callable[[datetime], float]:
    baseline = float(config.get("baseline", DEFAULT_BASELINES[name]))
    noise = float(config.get("noise", 0.0))
    fault = config.get("fault") or {}
    shape = fault.get("shape", "none")
    at = float(fault.get("at", 0))
    level = float(fault.get("level", baseline))

    def value(ts: datetime) -> float:
        offset = (ts - FAILURE_TIME).total_seconds()
        normal = baseline + (noise if int(offset // 15) % 2 else -noise)
        if shape == "step":
            return level if offset >= at else normal
        if shape == "spike":
            return level if at <= offset < at + float(fault["duration"]) else normal
        if shape == "ramp":
            until = float(fault.get("until", 60))
            if offset < at:
                return normal
            if offset >= until:
                return level
            return normal + (level - normal) * (offset - at) / (until - at)
        return normal

    return value


class ScenarioTool(FakeTool):
    """FakeTool whose series come from a scenario; also supports series that stop early."""

    def __init__(self, scenario: dict[str, Any]) -> None:
        overrides: dict[str, Any] = {}
        self._stops_at: dict[str, float] = {}
        for name, config in (scenario.get("metrics") or {}).items():
            promql = _PROMQL_BY_NAME[name]
            shape = (config.get("fault") or {}).get("shape")
            overrides[promql] = None if shape == "missing" else _series_fn(name, config)
            if shape == "stops":
                self._stops_at[promql] = float(config["fault"]["at"])
        unavailable = (scenario.get("prometheus") or {}).get("unavailable", False)
        errors = {spec.promql: PrometheusUnavailableError("scenario: prometheus down") for spec in CATALOG}
        super().__init__(overrides, errors if unavailable else None)

    def query_range(self, promql: str, start: datetime, end: datetime, step: str) -> QueryResult:
        result = super().query_range(promql, start, end, step)
        if promql in self._stops_at and result.series:
            cutoff = FAILURE_TIME + timedelta(seconds=self._stops_at[promql])
            kept = [s for s in result.series[0].samples if s.timestamp <= cutoff]
            return QueryResult(promql=promql, result_type="matrix", series=[Series(labels={}, samples=kept)])
        return result


def _event(scenario: dict[str, Any]) -> IncidentCreatedEvent:
    incident = scenario["incident"]
    return IncidentCreatedEvent.model_validate(
        {
            "incident_id": str(uuid4()),
            "job_name": "target-app",
            "build_number": 1,
            "build_url": "http://jenkins:8080/job/target-app/1/",
            "branch": incident.get("branch", "main"),
            "git_commit": incident.get("git_commit", "0" * 40),
            "failed_stage": incident.get("failed_stage"),
            "timestamp": FAILURE_TIME,
        }
    )


def investigate(scenario: dict[str, Any], tmp_path: Path) -> tuple[Evidence, list[AuditRecord], Evidence | None]:
    """Run the real agent on the scenario; return (evidence, audit trail, evidence read back from the store)."""
    store = FileEvidenceStore(tmp_path / "evidence")
    audit = FileAuditLog(tmp_path / "audit")
    investigator = Investigator(
        ScenarioTool(scenario),  # type: ignore[arg-type]
        CatalogPlanner(),
        SharedStoreEvidenceWriter(store),
        InvestigationConfig(settle=timedelta(0)),
        clock=lambda: NOW,
        sleep=lambda _seconds: None,
    )
    event = _event(scenario)

    async def run() -> tuple[Evidence, list[AuditRecord], Evidence | None]:
        evidence = await MetricsAgent(investigator, audit).run_event(event)
        return evidence, await audit.read(event.incident_id), await store.read(event.incident_id, AGENT)

    return asyncio.run(run())
