"""Synthetic series and windows shared by the Metrics agent tests."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from agents.metrics_agent.models import QueryResult, Sample, Series
from agents.metrics_agent.window import InvestigationWindow, build_window

FAILURE_TIME = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)
NOW = FAILURE_TIME + timedelta(minutes=2)
STEP = timedelta(seconds=15)


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
