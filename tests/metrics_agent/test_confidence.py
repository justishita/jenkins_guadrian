"""Confidence reflects evidence quality, not just anomaly magnitude.

These are the required properties of the formula in `agents/metrics_agent/confidence.py`:
each one holds everything else equal, so a failure points at exactly one input.
"""

import itertools

import pytest

from agents.metrics_agent.confidence import (
    CONFIDENCE_CEILING,
    NORMAL_MAX,
    ConfidenceInputs,
    closeness,
    corroboration_adjustment,
    final_confidence,
    normal_confidence,
    persistence,
)


def inputs(strength: float = 0.8, run: int = 4, quality: float = 1.0, gap: float | None = 0.0) -> ConfidenceInputs:
    return ConfidenceInputs(strength=strength, persistence=persistence(run), quality=quality, closeness=closeness(gap))


def test_a_huge_spike_long_before_the_failure_does_not_beat_a_moderate_spike_at_the_failure() -> None:
    huge_but_distant = final_confidence(inputs(strength=1.0, run=8, gap=225))
    moderate_at_failure = final_confidence(inputs(strength=0.5, run=4, gap=0))
    assert moderate_at_failure > huge_but_distant


def test_closer_to_the_failure_is_more_confident() -> None:
    assert final_confidence(inputs(gap=0)) > final_confidence(inputs(gap=60)) > final_confidence(inputs(gap=240))


def test_persistent_evidence_beats_the_minimum_run() -> None:
    assert final_confidence(inputs(run=2)) < final_confidence(inputs(run=3)) < final_confidence(inputs(run=4))
    assert final_confidence(inputs(run=4)) == final_confidence(inputs(run=40))  # persistence saturates


def test_stronger_anomalies_are_more_confident() -> None:
    assert final_confidence(inputs(strength=0.2)) < final_confidence(inputs(strength=0.9))


def test_poor_data_quality_lowers_confidence() -> None:
    assert final_confidence(inputs(quality=0.4)) < final_confidence(inputs(quality=1.0))


def test_corroboration_raises_and_contradiction_lowers_confidence() -> None:
    base = final_confidence(inputs(strength=0.5))
    assert final_confidence(inputs(strength=0.5), corroborating=1) > base
    assert final_confidence(inputs(strength=0.5), contradicting=1) < base


def test_corroboration_and_contradiction_are_capped() -> None:
    assert corroboration_adjustment(10, 0) == pytest.approx(0.15)
    assert corroboration_adjustment(0, 10) == pytest.approx(-0.10)
    assert corroboration_adjustment(0, 0) == 0


def test_confidence_always_stays_within_zero_and_the_ceiling() -> None:
    for strength, run, quality, gap, corr, contra in itertools.product(
        (0.0, 0.5, 1.0), (2, 4), (0.0, 1.0), (None, 0.0, 500.0), (0, 5), (0, 5)
    ):
        value = final_confidence(inputs(strength, run, quality, gap), corr, contra)
        assert 0.0 <= value <= CONFIDENCE_CEILING


def test_even_perfect_corroborated_evidence_never_claims_certainty() -> None:
    assert final_confidence(inputs(strength=1.0, run=10, quality=1.0, gap=0), corroborating=5) == CONFIDENCE_CEILING


def test_persistence_scale() -> None:
    assert persistence(1) == 0
    assert persistence(2) == pytest.approx(1 / 3)
    assert persistence(4) == 1.0 and persistence(50) == 1.0


def test_closeness_halves_every_two_minutes() -> None:
    assert closeness(0) == 1.0
    assert closeness(120) == pytest.approx(0.5)
    assert closeness(None) == 0.0


def test_normal_metrics_give_low_confidence_scaled_by_data_quality() -> None:
    assert normal_confidence([1.0, 1.0]) == NORMAL_MAX
    assert normal_confidence([0.5, 0.5]) == pytest.approx(NORMAL_MAX / 2)
    assert normal_confidence([]) == 0.0
