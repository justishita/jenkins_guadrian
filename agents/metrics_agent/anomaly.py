"""Deterministic, explainable anomaly detection over a baseline and an incident window."""

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

CONFIDENCE_FLOOR = 0.6
CONFIDENCE_CEILING = 0.95


class Detection(BaseModel):
    detector: DetectorKind
    anomalous: bool
    direction: Literal["up", "down", "none"]
    baseline_value: float
    observed_value: float
    # Detector-specific strength: z-score, increase / min_effect, or fraction of samples down.
    score: float
    confidence: float
    window_start: datetime
    window_end: datetime
    detail: str


def _confidence(ratio: float) -> float:
    """Map how far past the threshold we are (ratio >= 1) to a 0.6..0.95 confidence."""
    ratio = max(ratio, 1.0)
    return min(CONFIDENCE_CEILING, CONFIDENCE_FLOOR + (CONFIDENCE_CEILING - CONFIDENCE_FLOOR) * (1 - 1 / ratio))


def _spike(spec: QuerySpec, baseline: list[Sample], incident: list[Sample]) -> Detection:
    base = [s.value for s in baseline]
    center = median(base)
    mad = median(abs(v - center) for v in base)
    # Floor sigma so a perfectly flat baseline cannot yield an infinite z-score.
    sigma = max(_MAD_TO_SIGMA * mad, 0.1 * abs(center), spec.min_effect / spec.z_threshold)
    peak = max(s.value for s in incident)
    delta = peak - center
    z = delta / sigma
    anomalous = z >= spec.z_threshold and delta >= spec.min_effect
    return Detection(
        detector=DetectorKind.SPIKE,
        anomalous=anomalous,
        direction="up" if anomalous else "none",
        baseline_value=center,
        observed_value=peak,
        score=z,
        confidence=_confidence(z / spec.z_threshold) if anomalous else 0.0,
        window_start=incident[0].timestamp,
        window_end=incident[-1].timestamp,
        detail=f"peak {peak:.4g} vs baseline median {center:.4g} (z={z:.1f}, threshold {spec.z_threshold:g})",
    )


def _sustained_increase(spec: QuerySpec, baseline: list[Sample], incident: list[Sample]) -> Detection:
    center = median(s.value for s in baseline)
    values = [s.value for s in incident]
    last = values[-1]
    delta = last - center
    steps = list(pairwise(values))
    tolerance = _JITTER_FRACTION * abs(center)
    non_decreasing = sum(b - a >= -tolerance for a, b in steps) / len(steps) if steps else 1.0
    relative = delta / center if center else float("inf")
    anomalous = (
        delta >= spec.min_effect and relative >= _MIN_RELATIVE_INCREASE and non_decreasing >= _MIN_NON_DECREASING
    )
    score = delta / spec.min_effect
    return Detection(
        detector=DetectorKind.SUSTAINED_INCREASE,
        anomalous=anomalous,
        direction="up" if anomalous else "none",
        baseline_value=center,
        observed_value=last,
        score=score,
        confidence=_confidence(score) if anomalous else 0.0,
        window_start=incident[0].timestamp,
        window_end=incident[-1].timestamp,
        detail=(
            f"rose {delta:.4g} {spec.unit} over baseline median {center:.4g} "
            f"({non_decreasing:.0%} of steps non-decreasing)"
        ),
    )


def _availability(spec: QuerySpec, baseline: list[Sample], incident: list[Sample]) -> Detection:
    down = sum(s.value == 0 for s in incident)
    fraction = down / len(incident)
    anomalous = down > 0
    return Detection(
        detector=DetectorKind.AVAILABILITY,
        anomalous=anomalous,
        direction="down" if anomalous else "none",
        baseline_value=median(s.value for s in baseline),
        observed_value=min(s.value for s in incident),
        score=fraction,
        confidence=CONFIDENCE_FLOOR + (CONFIDENCE_CEILING - CONFIDENCE_FLOOR) * fraction if anomalous else 0.0,
        window_start=incident[0].timestamp,
        window_end=incident[-1].timestamp,
        detail=f"target down in {down} of {len(incident)} samples",
    )


_DETECTORS = {
    DetectorKind.SPIKE: _spike,
    DetectorKind.SUSTAINED_INCREASE: _sustained_increase,
    DetectorKind.AVAILABILITY: _availability,
}


def detect(spec: QuerySpec, baseline: list[Sample], incident: list[Sample]) -> Detection:
    """Run the detector for `spec`. Callers must pass sanity-checked, non-empty inputs."""
    if not baseline or not incident:
        raise ValueError("detect() requires non-empty baseline and incident samples")
    return _DETECTORS[spec.detector](spec, baseline, incident)
