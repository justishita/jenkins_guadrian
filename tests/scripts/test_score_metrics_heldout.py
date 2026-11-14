"""The held-out random cases of scripts/score_metrics_agent.py: repeatable, correctly labelled, honestly summarised."""

import logging
import math

import pytest

from scripts import score_metrics_agent as sma

TRUTH_BY_KIND = {
    "healthy": sma.UNKNOWN, "slow": sma.TIMEOUT, "cpu": sma.RESOURCE, "leak": sma.RESOURCE,
    "mixed": sma.RESOURCE, "down": sma.INFRA, "glitch": sma.UNKNOWN,
}


def test_the_same_seed_gives_the_same_cases_and_another_seed_different_ones():
    first, again, other = sma.heldout_cases(40, 1), sma.heldout_cases(40, 1), sma.heldout_cases(40, 2)
    assert first == again and first != other


def test_every_case_is_labelled_by_what_was_injected():
    cases = sma.heldout_cases(300, 5)
    assert len(cases) == 300
    assert {kind for kind, *_ in cases} == set(TRUTH_BY_KIND)  # every kind appears
    for kind, label, scenario, truth in cases:
        assert truth == TRUTH_BY_KIND[kind], label
        assert scenario["metrics"], label


def test_the_mix_leans_healthy_and_slow_like_real_incidents():
    kinds = [kind for kind, *_ in sma.heldout_cases(2000, 9)]
    assert kinds.count("healthy") > kinds.count("cpu") and kinds.count("slow") > kinds.count("glitch")


def test_fault_magnitudes_stay_inside_the_ranges_the_labels_assume():
    for kind, _, scenario, _ in sma.heldout_cases(400, 3):
        metrics = scenario["metrics"]
        if kind == "slow":
            assert 0.25 <= metrics["latency_p95"]["fault"]["level"] <= 4.0
        if kind == "leak":
            fault = metrics["memory_rss"]["fault"]
            assert fault["at"] < fault["until"] and fault["level"] > 80 * sma.MIB


def test_wilson_interval_is_wide_for_few_samples_and_tight_for_many():
    low, high = sma.wilson_interval(1, 1)
    assert low < 0.25 and high == 1.0
    low, high = sma.wilson_interval(190, 200)
    assert 0.91 < low < 0.93 and 0.96 < high < 0.98
    assert all(math.isnan(v) for v in sma.wilson_interval(0, 0))


@pytest.fixture(scope="module")
def small_run():
    logging.disable(logging.CRITICAL)
    try:
        return sma.run_heldout(sma.Thresholds(), 30, 11)
    finally:
        logging.disable(logging.NOTSET)


def test_a_run_scores_the_agent_and_the_baseline_on_the_same_cases(small_run):
    assert small_run["n"] == 30 and small_run["agent"]["n"] == small_run["baseline"]["n"] == 30
    assert sum(g["n"] for g in small_run["per_kind"].values()) == 30
    low, high = small_run["agent_ci95"]
    assert 0 <= low <= small_run["agent"]["accuracy"] <= high <= 1


def test_the_agent_beats_the_naive_thresholds_on_unseen_cases(small_run):
    assert small_run["agent"]["accuracy"] > small_run["baseline"]["accuracy"]


def test_late_leaks_are_found_not_dismissed_as_unknown():
    """The regression that the held-out set exposed: leaks that began in the last ~3 minutes were missed."""
    logging.disable(logging.CRITICAL)
    try:
        leaks = [c for c in sma.heldout_cases(300, 2026) if c[0] == "leak"]
        late = [c for c in leaks if c[2]["metrics"]["memory_rss"]["fault"]["at"] > -230]
        assert len(late) >= 10
        missed = [label for _, label, scenario, truth in late if sma.agent_predict(scenario)[0] != truth]
    finally:
        logging.disable(logging.NOTSET)
    assert missed == []
