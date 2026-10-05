from datetime import datetime

import pytest

from agents.metrics_agent.anomaly import CONFIDENCE_CEILING, CONFIDENCE_FLOOR, detect
from agents.metrics_agent.models import Sample
from agents.metrics_agent.queries import AVAILABILITY, CPU_RATE, LATENCY_P95, MEMORY_RSS
from tests.metrics_agent.helpers import flat, make_window, samples

MIB = 1024 * 1024


def split(fn):  # type: ignore[no-untyped-def]
    window = make_window()
    baseline = samples(window.baseline_start, window.start, fn)
    incident = samples(window.start, window.end, fn, inclusive_end=True)
    return window, baseline, incident


def noisy(level: float, wobble: float):  # type: ignore[no-untyped-def]
    return lambda ts: level + (wobble if (ts.minute + ts.second // 15) % 2 else -wobble)


def test_latency_spike_detected_with_high_confidence() -> None:
    window = make_window()
    midpoint = window.start + (window.end - window.start) / 2
    fn = lambda ts: 2.0 if ts >= midpoint else 0.007 + (0.001 if ts.second % 30 else 0)
    _, base, inc = split(fn)
    detection = detect(LATENCY_P95, base, inc)
    assert detection.anomalous and detection.direction == "up"
    assert detection.observed_value == 2.0
    assert CONFIDENCE_FLOOR < detection.confidence <= CONFIDENCE_CEILING


def test_flat_healthy_series_not_anomalous() -> None:
    _, base, inc = split(flat(0.007))
    detection = detect(LATENCY_P95, base, inc)
    assert not detection.anomalous and detection.direction == "none" and detection.confidence == 0.0


def test_noisy_but_normal_series_not_anomalous() -> None:
    _, base, inc = split(noisy(0.5, 0.05))
    assert not detect(CPU_RATE, base, inc).anomalous


def test_bump_below_min_effect_ignored_even_on_flat_baseline() -> None:
    window = make_window()
    midpoint = window.start + (window.end - window.start) / 2
    # +0.1s is far above a flat baseline's noise but below the 0.25s that matters operationally.
    _, base, inc = split(lambda ts: 0.107 if ts >= midpoint else 0.007)
    assert not detect(LATENCY_P95, base, inc).anomalous


def test_cpu_burn_detected() -> None:
    window = make_window()
    midpoint = window.start + (window.end - window.start) / 2
    _, base, inc = split(lambda ts: 0.9 if ts >= midpoint else 0.002)
    assert detect(CPU_RATE, base, inc).anomalous


def test_memory_step_up_is_sustained_increase() -> None:
    window = make_window()
    midpoint = window.start + (window.end - window.start) / 2
    _, base, inc = split(lambda ts: 100 * MIB if ts >= midpoint else 80 * MIB)
    detection = detect(MEMORY_RSS, base, inc)
    assert detection.anomalous and detection.direction == "up"
    assert detection.observed_value == 100 * MIB


def test_memory_ramp_detected() -> None:
    window = make_window()
    seconds = lambda ts: (ts - window.start).total_seconds()
    _, base, inc = split(lambda ts: 80 * MIB + max(0.0, seconds(ts)) * 0.1 * MIB)
    assert detect(MEMORY_RSS, base, inc).anomalous


def test_memory_flat_not_anomalous() -> None:
    _, base, inc = split(flat(80 * MIB))
    assert not detect(MEMORY_RSS, base, inc).anomalous


def test_memory_sawtooth_not_a_leak_even_if_it_ends_high() -> None:
    window = make_window()
    # GC-style oscillation: ends 20 MiB above baseline but keeps dropping.
    _, base, inc = split(lambda ts: (100 if ts.second // 15 % 2 == 0 else 70) * MIB if ts >= window.start else 80 * MIB)
    assert not detect(MEMORY_RSS, base, inc).anomalous


def test_availability_detects_any_downtime() -> None:
    window = make_window()
    midpoint = window.start + (window.end - window.start) / 2
    _, base, inc = split(lambda ts: 0.0 if ts >= midpoint else 1.0)
    detection = detect(AVAILABILITY, base, inc)
    assert detection.anomalous and detection.direction == "down"
    assert 0 < detection.score <= 1


def test_availability_all_up_is_normal() -> None:
    _, base, inc = split(flat(1.0))
    assert not detect(AVAILABILITY, base, inc).anomalous


def test_detect_requires_data() -> None:
    sample = Sample(timestamp=datetime(2026, 10, 5, tzinfo=make_window().start.tzinfo), value=1.0)
    with pytest.raises(ValueError):
        detect(CPU_RATE, [], [sample])
    with pytest.raises(ValueError):
        detect(CPU_RATE, [sample], [])
