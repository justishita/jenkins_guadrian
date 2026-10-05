from collections.abc import Callable
from datetime import datetime

import pytest

from agents.metrics_agent.anomaly import MIN_CONSECUTIVE, Detection, detect
from agents.metrics_agent.models import Sample
from agents.metrics_agent.queries import (
    AVAILABILITY,
    CPU_RATE,
    LATENCY_P95,
    MEMORY_RSS,
    QuerySpec,
)
from tests.metrics_agent.helpers import FAILURE_TIME, MIB, flat, make_window, samples

Fn = Callable[[datetime], float]


def split(fn: Fn) -> tuple[list[Sample], list[Sample]]:
    window = make_window()
    baseline = samples(window.baseline_start, window.start, fn)
    incident = samples(window.start, window.end, fn, inclusive_end=True)
    return baseline, incident


def run(spec: QuerySpec, fn: Fn) -> Detection:
    baseline, incident = split(fn)
    return detect(spec, baseline, incident, FAILURE_TIME)


def offset(ts: datetime) -> float:
    return (ts - FAILURE_TIME).total_seconds()


def burst(level: float, start: float, end: float, normal: float) -> Fn:
    """`level` while start <= offset < end (seconds from the failure), `normal` otherwise."""
    return lambda ts: level if start <= offset(ts) < end else normal


def noisy(level: float, wobble: float) -> Fn:
    return lambda ts: level + (wobble if int(offset(ts) // 15) % 2 else -wobble)


# --- spike detector ---------------------------------------------------------------------


def test_sustained_latency_step_is_detected_with_persistence_facts() -> None:
    detection = run(LATENCY_P95, burst(2.0, -120, 1000, 0.007))
    assert detection.anomalous and detection.direction == "up"
    assert detection.observed_value == 2.0
    assert detection.strength > 0.85
    assert detection.longest_run >= 4 and detection.elevated_samples == detection.longest_run
    assert detection.still_elevated and detection.gap_to_failure_seconds == 0


def test_flat_healthy_series_is_not_anomalous() -> None:
    detection = run(LATENCY_P95, flat(0.007))
    assert not detection.anomalous and detection.direction == "none"
    assert detection.strength == 0.0 and detection.elevated_samples == 0
    assert detection.gap_to_failure_seconds is None


def test_noisy_but_normal_series_is_not_anomalous() -> None:
    assert not run(CPU_RATE, noisy(0.5, 0.05)).anomalous


def test_bump_below_min_effect_is_ignored_even_on_a_flat_baseline() -> None:
    # +0.1s is far above a flat baseline's noise but below the 0.25s that matters operationally.
    assert not run(LATENCY_P95, burst(0.107, -120, 1000, 0.007)).anomalous


def test_single_elevated_sample_is_not_an_anomaly() -> None:
    detection = run(LATENCY_P95, burst(2.0, -30, -15, 0.007))
    assert detection.elevated_samples == 1 and detection.longest_run == 1
    assert not detection.anomalous
    assert f"{MIN_CONSECUTIVE} required" in detection.detail


def test_two_consecutive_elevated_samples_are_enough() -> None:
    detection = run(LATENCY_P95, burst(2.0, -30, 0, 0.007))
    assert detection.longest_run == MIN_CONSECUTIVE and detection.anomalous


def test_two_separated_elevated_samples_are_not_consecutive() -> None:
    fn = lambda ts: 2.0 if offset(ts) in (-120, -60) else 0.007
    detection = run(LATENCY_P95, fn)
    assert detection.elevated_samples == 2 and detection.longest_run == 1
    assert not detection.anomalous


def test_cpu_burn_is_detected() -> None:
    assert run(CPU_RATE, burst(0.9, -120, 1000, 0.002)).anomalous


def test_spike_that_recovered_before_the_failure_is_flagged_as_recovered() -> None:
    detection = run(LATENCY_P95, burst(2.0, -270, -210, 0.007))
    assert detection.anomalous and not detection.still_elevated
    assert detection.gap_to_failure_seconds == pytest.approx(225)  # last elevated sample at -225s
    assert "recovered 225s before the failure" in detection.detail


def test_stronger_anomalies_have_greater_strength() -> None:
    mild = run(LATENCY_P95, burst(0.5, -120, 1000, 0.007))
    severe = run(LATENCY_P95, burst(5.0, -120, 1000, 0.007))
    assert 0 < mild.strength < severe.strength <= 1


# --- sustained increase (memory) --------------------------------------------------------


def test_memory_step_up_is_a_sustained_increase() -> None:
    detection = run(MEMORY_RSS, burst(100 * MIB, -120, 1000, 80 * MIB))
    assert detection.anomalous and detection.direction == "up"
    assert detection.observed_value == 100 * MIB and detection.still_elevated


def test_memory_ramp_is_detected() -> None:
    fn = lambda ts: 80 * MIB + max(0.0, offset(ts) + 300) * 0.1 * MIB
    assert run(MEMORY_RSS, fn).anomalous


def test_flat_memory_is_not_anomalous() -> None:
    assert not run(MEMORY_RSS, flat(80 * MIB)).anomalous


def test_memory_sawtooth_is_not_a_leak_even_if_it_ends_high() -> None:
    # GC-style oscillation: ends 20 MiB above baseline but keeps dropping.
    fn = lambda ts: (100 if int(offset(ts) // 15) % 2 == 0 else 70) * MIB if offset(ts) >= -300 else 80 * MIB
    assert not run(MEMORY_RSS, fn).anomalous


def test_a_single_high_final_memory_sample_is_not_a_leak() -> None:
    assert not run(MEMORY_RSS, burst(120 * MIB, 60, 1000, 80 * MIB)).anomalous


# --- availability -----------------------------------------------------------------------


def test_availability_needs_consecutive_missed_scrapes() -> None:
    outage = run(AVAILABILITY, burst(0.0, -120, 1000, 1.0))
    assert outage.anomalous and outage.direction == "down"
    assert 0 < outage.score <= 1 and outage.strength > 0.5


def test_one_missed_scrape_is_not_an_outage() -> None:
    assert not run(AVAILABILITY, burst(0.0, -30, -15, 1.0)).anomalous


def test_availability_all_up_is_normal() -> None:
    assert not run(AVAILABILITY, flat(1.0)).anomalous


# --- contract ---------------------------------------------------------------------------


def test_detect_requires_data() -> None:
    sample = Sample(timestamp=FAILURE_TIME, value=1.0)
    with pytest.raises(ValueError):
        detect(CPU_RATE, [], [sample], FAILURE_TIME)
    with pytest.raises(ValueError):
        detect(CPU_RATE, [sample], [], FAILURE_TIME)
