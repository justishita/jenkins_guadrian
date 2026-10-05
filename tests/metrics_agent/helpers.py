"""Synthetic series and windows shared by the Metrics agent tests."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jsonschema.validators import validator_for

from agents.metrics_agent.models import QueryResult, Sample, Series
from agents.metrics_agent.queries import AVAILABILITY, CPU_RATE, LATENCY_P95, MEMORY_RSS
from agents.metrics_agent.window import InvestigationWindow, build_window

CANONICAL_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "common" / "evidence_schema.json"
FAILURE_TIME = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)
NOW = FAILURE_TIME + timedelta(minutes=2)
STEP = timedelta(seconds=15)
MIB = 1024 * 1024
Fn = Callable[[datetime], float]


def make_window() -> InvestigationWindow:
    """baseline 09:15-09:25, incident 09:25-09:31."""
    return build_window(
        FAILURE_TIME,
        lookback=timedelta(minutes=5),
        tail=timedelta(seconds=60),
        baseline=timedelta(minutes=10),
        now=NOW,
    )


def samples(
    start: datetime, end: datetime, value: Callable[[datetime], float], inclusive_end: bool = False
) -> list[Sample]:
    out: list[Sample] = []
    ts = start
    while ts < end or (inclusive_end and ts == end):
        out.append(Sample(timestamp=ts, value=value(ts)))
        ts += STEP
    return out


def result_from(window: InvestigationWindow, value: Callable[[datetime], float], promql: str = "q") -> QueryResult:
    points = samples(window.baseline_start, window.end, value, inclusive_end=True)
    return QueryResult(promql=promql, result_type="matrix", series=[Series(labels={}, samples=points)])


def flat(level: float) -> Callable[[datetime], float]:
    return lambda _ts: level


def step_up(window: InvestigationWindow, before: float, after: float) -> Callable[[datetime], float]:
    """`before` until the incident window starts, `after` from the middle of it on."""
    midpoint = window.start + (window.end - window.start) / 2
    return lambda ts: after if ts >= midpoint else before


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
        return QueryResult(
            promql=promql,
            result_type="matrix",
            series=[Series(labels={}, samples=samples(start, end, fn, inclusive_end=True))],
        )


def validate_against_canonical_schema(document: dict[str, Any]) -> None:
    """Raise `jsonschema.ValidationError` if `document` breaks the canonical evidence contract.

    This checks the JSON that actually crosses the boundary, independently of the Pydantic model
    that produced it, against `common/evidence_schema.json` (P3's generated, canonical schema).
    """
    schema = json.loads(CANONICAL_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator_for(schema)(schema).validate(document)
