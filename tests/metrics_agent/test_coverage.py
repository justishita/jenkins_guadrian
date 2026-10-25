"""The Metrics agent declares how much of its investigation produced usable data.

``investigation_coverage`` is what lets the Coordinator tell "looked and found nothing"
(healthy, full coverage) from "could not look" (no data, zero coverage).
"""

import pytest

from tests.metrics_agent.scenario_runner import (
    AGENT,
    investigate,
    load_metrics_scenarios,
    scenario_id,
)

SCENARIOS = {scenario_id(s): s for s in load_metrics_scenarios()}


@pytest.mark.parametrize(
    ("scenario", "coverage"),
    [
        ("metrics-healthy", 1.0),
        ("metrics-noisy-normal", 1.0),
        ("metrics-latency-spike", 1.0),
        ("metrics-no-data", 0.0),
        ("metrics-partial-data", 0.75),
    ],
)
def test_coverage_reflects_usable_data_not_whether_something_was_found(tmp_path, scenario, coverage) -> None:
    evidence, _audit, stored = investigate(SCENARIOS[scenario], tmp_path)
    assert evidence.agent == AGENT
    assert evidence.investigation_coverage == pytest.approx(coverage)
    assert stored is not None and stored.investigation_coverage == evidence.investigation_coverage


def test_no_data_explains_why_coverage_is_zero(tmp_path) -> None:
    evidence, _audit, _stored = investigate(SCENARIOS["metrics-no-data"], tmp_path)
    assert len(evidence.coverage_notes) == 4 and all("unusable" in n for n in evidence.coverage_notes)


def test_a_failed_investigation_declares_no_coverage(tmp_path) -> None:
    evidence, _audit, _stored = investigate(SCENARIOS["metrics-prometheus-unavailable"], tmp_path)
    assert evidence.status == "failed" and evidence.investigation_coverage is None
