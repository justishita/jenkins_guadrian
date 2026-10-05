"""Deterministic, explainable per-metric anomaly detection.

Each detector looks at one metric: a baseline window (normal behaviour) and an incident
window (around the failure). It reports *facts* about the anomaly - how strong, how
persistent, how close to the failure - and leaves turning those into a confidence to
`confidence.py`, and relating several metrics to `correlation.py`.

A single elevated sample is never an anomaly: a metric must stay elevated for at least
`MIN_CONSECUTIVE` consecutive samples, so one noisy scrape cannot name a root cause.
"""

from collections.abc import Sequence
from datetime import datetime
from itertools import pairwise
from statistics import median
from typing import Literal

from pydantic import BaseModel

from .models import Sample
from .queries import DetectorKind, QuerySpec

# Scale factor turning a median absolute deviation into a standard-deviation estimate.
_MAD_TO_SIGMA = 1.4826
# Fraction of consecutive steps allowed to dip (RSS jitter) in a sustained increase.
_MIN_NON_DECREASING = 0.9
_JITTER_FRACTION = 0.001
_MIN_RELATIVE_INCREASE = 0.05

#: Consecutive elevated samples required before a metric counts as anomalous.
MIN_CONSECUTIVE = 2


class Detection(BaseModel):
    detector: DetectorKind
    anomalous: bool
    direction: Literal["up", "down", "none"]
    baseline_value: float
    observed_value: float
    # Detector-specific magnitude, kept for explanation: peak z-score, increase / min_effect,
    # or fraction of samples down.
    score: float
    # How far past the threshold the anomaly is, squashed to 0..1 (0 = barely, ->1 = far past).
    strength: float
    # Persistence and timing, all measured on the incident window. `elevated_samples` and
    # `longest_run` look at the whole window; `run_start_at`/`run_end_at` bound the MOST RECENT
    # contiguous elevated stretch, the one nearest the failure, which is what correlation uses.
    elevated_samples: int
    longest_run: int
    incident_samples: int
    run_start_at: datetime | None
    run_end_at: datetime | None
    # Seconds between the end of that latest stretch and the failure (0 if it reaches the failure).
    gap_to_failure_seconds: float | None
    still_elevated: bool
    window_start: datetime
    window_end: datetime
    detail: str


def _strength(ratio: float) -> float:
    """Map 'how many times the threshold' (>= 1 when anomalous) to 0..1; 1x -> 0, 2x -> 0.5, 10x -> 0.9."""
    ratio = max(ratio, 1.0)
    return 1 - 1 / ratio


def _longest_run(flags: Sequence[bool]) -> int:
    best = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        best = max(best, current)
    return best


class _Timing(BaseModel):
    run_start: datetime | None
    run_end: datetime | None
    gap_seconds: float | None
    still_elevated: bool


def _timing(incident: list[Sample], flags: Sequence[bool], failure_time: datetime) -> _Timing:
    """Timing of the most recent contiguous elevated stretch (a metric can be elevated more than once)."""
    if not any(flags):
        return _Timing(run_start=None, run_end=None, gap_seconds=None, still_elevated=False)
    end = max(i for i, flag in enumerate(flags) if flag)
    start = end
    while start > 0 and flags[start - 1]:
        start -= 1
    run_end = incident[end].timestamp
    return _Timing(
        run_start=incident[start].timestamp,
        run_end=run_end,
        gap_seconds=max(0.0, (failure_time - run_end).total_seconds()),
        still_elevated=bool(flags[-1]),
    )


def _build(
    spec: QuerySpec,
    incident: list[Sample],
    flags: Sequence[bool],
    failure_time: datetime,
    *,
    anomalous: bool,
    direction: Literal["up", "down"],
    baseline_value: float,
    observed_value: float,
    score: float,
    ratio: float,
    detail: str,
) -> Detection:
    timing = _timing(incident, flags, failure_time)
    run = _longest_run(flags)
    if anomalous and not timing.still_elevated and timing.gap_seconds:
        detail += f"; recovered {timing.gap_seconds:.0f}s before the failure"
    elif not anomalous and any(flags):
        detail += f"; only {run} consecutive elevated sample(s), {MIN_CONSECUTIVE} required"
    return Detection(
        detector=spec.detector,
        anomalous=anomalous,
        direction=direction if anomalous else "none",
        baseline_value=baseline_value,
        observed_value=observed_value,
        score=score,
        strength=_strength(ratio) if anomalous else 0.0,
        elevated_samples=sum(flags),
        longest_run=run,
        incident_samples=len(incident),
        run_start_at=timing.run_start,
        run_end_at=timing.run_end,
        gap_to_failure_seconds=timing.gap_seconds,
        still_elevated=timing.still_elevated,
        window_start=incident[0].timestamp,
        window_end=incident[-1].timestamp,
        detail=detail,
    )


def _spike(spec: QuerySpec, baseline: list[Sample], incident: list[Sample], failure_time: datetime) -> Detection:
    base = [s.value for s in baseline]
    center = median(base)
    mad = median(abs(v - center) for v in base)
    # Floor sigma so a perfectly flat baseline cannot yield an infinite z-score.
    sigma = max(_MAD_TO_SIGMA * mad, 0.1 * abs(center), spec.min_effect / spec.z_threshold)
    deltas = [s.value - center for s in incident]
    flags = [d / sigma >= spec.z_threshold and d >= spec.min_effect for d in deltas]
    peak = max(s.value for s in incident)
    z = max(deltas) / sigma
    return _build(
        spec,
        incident,
        flags,
        failure_time,
        anomalous=_longest_run(flags) >= MIN_CONSECUTIVE,
        direction="up",
        baseline_value=center,
        observed_value=peak,
        score=z,
        ratio=z / spec.z_threshold,
        detail=f"peak {peak:.4g} vs baseline median {center:.4g} (z={z:.1f}, threshold {spec.z_threshold:g})",
    )


def _sustained_increase(
    spec: QuerySpec, baseline: list[Sample], incident: list[Sample], failure_time: datetime
) -> Detection:
    center = median(s.value for s in baseline)
    values = [s.value for s in incident]
    flags = [
        v - center >= spec.min_effect and (v - center) / center >= _MIN_RELATIVE_INCREASE if center else False
        for v in values
    ]
    last = values[-1]
    delta = last - center
    steps = list(pairwise(values))
    tolerance = _JITTER_FRACTION * abs(center)
    non_decreasing = sum(b - a >= -tolerance for a, b in steps) / len(steps) if steps else 1.0
    anomalous = _longest_run(flags) >= MIN_CONSECUTIVE and non_decreasing >= _MIN_NON_DECREASING and flags[-1]
    return _build(
        spec,
        incident,
        flags,
        failure_time,
        anomalous=anomalous,
        direction="up",
        baseline_value=center,
        observed_value=last,
        score=delta / spec.min_effect,
        ratio=delta / spec.min_effect,
        detail=(
            f"rose {delta:.4g} {spec.unit} over baseline median {center:.4g} "
            f"({non_decreasing:.0%} of steps non-decreasing)"
        ),
    )


def _availability(
    spec: QuerySpec, baseline: list[Sample], incident: list[Sample], failure_time: datetime
) -> Detection:
    flags = [s.value == 0 for s in incident]
    down = sum(flags)
    return _build(
        spec,
        incident,
        flags,
        failure_time,
        anomalous=_longest_run(flags) >= MIN_CONSECUTIVE,
        direction="down",
        baseline_value=median(s.value for s in baseline),
        observed_value=min(s.value for s in incident),
        score=down / len(incident),
        ratio=down / MIN_CONSECUTIVE,
        detail=f"target down in {down} of {len(incident)} samples",
    )


_DETECTORS = {
    DetectorKind.SPIKE: _spike,
    DetectorKind.SUSTAINED_INCREASE: _sustained_increase,
    DetectorKind.AVAILABILITY: _availability,
}


def detect(
    spec: QuerySpec, baseline: list[Sample], incident: list[Sample], failure_time: datetime
) -> Detection:
    """Run the detector for `spec`. Callers must pass sanity-checked, non-empty inputs."""
    if not baseline or not incident:
        raise ValueError("detect() requires non-empty baseline and incident samples")
    return _DETECTORS[spec.detector](spec, baseline, incident, failure_time)
