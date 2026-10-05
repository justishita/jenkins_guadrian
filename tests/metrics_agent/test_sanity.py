import math
from datetime import timedelta

from agents.metrics_agent.models import QueryResult, Series
from agents.metrics_agent.queries import CPU_RATE
from agents.metrics_agent.sanity import SanityIssue, check
from tests.metrics_agent.helpers import flat, make_window, result_from


def test_healthy_series_passes_and_is_split_at_window_start() -> None:
    window = make_window()
    outcome = check(result_from(window, flat(0.01)), CPU_RATE, window)
    assert outcome.ok and outcome.issues == []
    assert all(s.timestamp < window.start for s in outcome.baseline)
    assert all(window.start <= s.timestamp <= window.end for s in outcome.incident)
    assert len(outcome.baseline) == 40 and len(outcome.incident) == 25


def test_empty_result_cannot_conclude() -> None:
    window = make_window()
    empty = QueryResult(promql="q", result_type="matrix", series=[])
    outcome = check(empty, CPU_RATE, window)
    assert not outcome.ok and outcome.issues == [SanityIssue.EMPTY]


def test_non_finite_values_are_dropped_not_trusted() -> None:
    window = make_window()
    result = result_from(window, lambda ts: math.nan if ts.second == 0 else 0.01)
    outcome = check(result, CPU_RATE, window)
    assert outcome.dropped_non_finite > 0
    assert all(math.isfinite(s.value) for s in outcome.baseline + outcome.incident)


def test_too_few_baseline_samples() -> None:
    window = make_window()
    result = result_from(window, flat(0.01))
    result.series[0].samples = [s for s in result.series[0].samples if s.timestamp >= window.start]
    outcome = check(result, CPU_RATE, window)
    assert SanityIssue.TOO_FEW_BASELINE in outcome.issues and not outcome.ok


def test_too_few_incident_samples() -> None:
    window = make_window()
    result = result_from(window, flat(0.01))
    result.series[0].samples = [s for s in result.series[0].samples if s.timestamp < window.start]
    outcome = check(result, CPU_RATE, window)
    assert SanityIssue.TOO_FEW_INCIDENT in outcome.issues


def test_stale_data_flagged_when_series_stops_early() -> None:
    window = make_window()
    result = result_from(window, flat(0.01))
    cutoff = window.end - timedelta(minutes=3)
    result.series[0].samples = [s for s in result.series[0].samples if s.timestamp <= cutoff]
    outcome = check(result, CPU_RATE, window)
    assert SanityIssue.STALE in outcome.issues


def test_out_of_range_value_flagged() -> None:
    window = make_window()
    outcome = check(result_from(window, lambda ts: -1.0 if ts == window.start else 0.01), CPU_RATE, window)
    assert SanityIssue.OUT_OF_RANGE in outcome.issues and not outcome.ok


def test_most_populated_series_is_used() -> None:
    window = make_window()
    full = result_from(window, flat(0.01)).series[0]
    sparse = Series(labels={"x": "y"}, samples=full.samples[:2])
    outcome = check(QueryResult(promql="q", result_type="matrix", series=[sparse, full]), CPU_RATE, window)
    assert outcome.ok
