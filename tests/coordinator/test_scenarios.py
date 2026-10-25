"""Run the Coordinator against its scenarios and check the whole report structure.

Like the Metrics scenarios, each file declares every key of the expected report, so none can
quietly check less: verdict, fused score, completeness, missing/failed agents, conflict
status and types, unclassified pairs, the ranking, readiness, blocking reasons and the
negative-evidence contributions.
"""

import pytest

from tests.coordinator.scenario_runner import (
    load_coordinator_scenarios,
    run_scenario,
    scenario_id,
)

SCENARIOS = load_coordinator_scenarios()

TOP_LEVEL_KEYS = {"id", "name", "owner", "agent", "incident", "scenario", "truth", "agents", "expected", "config"}
EXPECTED_KEYS = {
    "verdict",
    "confidence",
    "completeness",
    "missing_agents",
    "failed_agents",
    "conflict_status",
    "conflicting_types",
    "unclassified_pairs",
    "ranking",
    "ready_for_policy_review",
    "blocking",
    "negative",
}


def test_scenarios_exist_for_every_planned_case() -> None:
    ids = {s["id"] for s in SCENARIOS}
    assert {
        "coord-all-agree",
        "coord-metrics-only-signal",
        "coord-conflict-close",
        "coord-compatible-close",
        "coord-partial-reporting",
        "coord-failed-agent",
        "coord-no-data-metrics",
        "coord-healthy-adequate-metrics",
        "coord-all-unknown",
    } <= ids


def test_scenario_ids_are_unique() -> None:
    ids = [s["id"] for s in SCENARIOS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_id)
def test_scenario_declares_the_whole_structure(scenario) -> None:
    assert set(scenario) - {"_file"} <= TOP_LEVEL_KEYS
    assert "truth" in scenario, "truth must be stated, as null when unknown"
    assert set(scenario["expected"]) == EXPECTED_KEYS


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_id)
def test_scenario(scenario) -> None:
    expected = scenario["expected"]
    report = run_scenario(scenario)

    assert report.verdict.value == expected["verdict"]
    assert report.confidence == pytest.approx(expected["confidence"], abs=1e-3)
    assert report.completeness.value == expected["completeness"]
    assert report.missing_agents == expected["missing_agents"]
    assert report.failed_agents == expected["failed_agents"]
    assert report.conflict_status.value == expected["conflict_status"]
    assert sorted(t.value for t in report.conflicting_types) == sorted(expected["conflicting_types"])
    assert [sorted(t.value for t in pair) for pair in report.unclassified_pairs] == [
        sorted(pair) for pair in expected["unclassified_pairs"]
    ]
    assert [s.failure_type.value for s in report.ranking] == expected["ranking"]
    assert report.ready_for_policy_review is expected["ready_for_policy_review"]
    assert [b.code.value for b in report.blocking_reasons] == expected["blocking"]

    negatives = [
        {"agent": n.agent, "failure_type": n.failure_type.value, "kind": n.kind.value, "status": n.status.value}
        for s in report.ranking
        for n in s.negative_contributions
    ]
    assert negatives == expected["negative"]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_id)
def test_report_is_deterministic(scenario) -> None:
    assert run_scenario(scenario) == run_scenario(scenario)
