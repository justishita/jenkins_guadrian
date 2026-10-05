"""Run the Metrics agent against its test-case scenarios.

Every `scenarios/*.yaml` with `agent: metrics_agent` is driven end to end through the real
agent (investigator, shared Evidence Store, audit trail) against a fake Prometheus, then
checked against its declared ground truth.

The check is deliberately the *whole evidence structure*, not just the failure type: status,
failure type, confidence, ranked hypotheses, supporting and contradicting evidence, the tool
calls (each scenario declares its own query plan), and the investigation window. A scenario
that omits one of those keys fails the meta tests, so none can quietly check less.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from common.models import Evidence
from tests.metrics_agent.helpers import FAILURE_TIME, validate_against_canonical_schema
from tests.metrics_agent.scenario_runner import (
    AGENT,
    investigate,
    load_metrics_scenarios,
    scenario_id,
)

SCENARIOS = load_metrics_scenarios()

TOP_LEVEL_KEYS = {"id", "name", "owner", "agent", "scenario", "incident", "metrics", "expected"}
EXPECTED_KEYS = {
    "status",
    "failure_type",
    "hypotheses",
    "supporting_evidence",
    "contradicting_evidence",
    "tool_calls",
    "window",
}
TOOL_CALL_KEYS = {"count", "ok", "expected_promql"}
WINDOW_KEYS = {"baseline_start", "incident_start", "end"}


# --- meta: the scenario files themselves ---------------------------------------


def test_metrics_scenarios_exist() -> None:
    assert SCENARIOS, "no scenarios with agent: metrics_agent found in scenarios/"


def test_tc12_is_covered_on_the_metrics_side() -> None:
    assert "TC-12" in {s["id"] for s in SCENARIOS}


def test_scenario_ids_are_unique() -> None:
    ids = [s["id"] for s in SCENARIOS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_id)
def test_every_scenario_declares_the_full_evidence_structure(scenario: dict[str, Any]) -> None:
    name = scenario["_file"]
    assert TOP_LEVEL_KEYS <= set(scenario), f"{name} is missing {TOP_LEVEL_KEYS - set(scenario)}"
    expected = scenario["expected"]
    assert EXPECTED_KEYS <= set(expected), f"{name} expects too little: missing {EXPECTED_KEYS - set(expected)}"
    assert "min_confidence" in expected or "max_confidence" in expected, f"{name} declares no confidence bound"
    assert TOOL_CALL_KEYS <= set(expected["tool_calls"]), f"{name}: tool_calls must declare {TOOL_CALL_KEYS}"
    assert WINDOW_KEYS <= set(expected["window"]), f"{name}: window must declare {WINDOW_KEYS}"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_id)
def test_declared_query_plan_is_self_consistent(scenario: dict[str, Any]) -> None:
    plan = scenario["expected"]["tool_calls"]
    assert plan["count"] == len(plan["expected_promql"]) == len(plan["ok"])


# --- behaviour: the real agent against each scenario -----------------------------


def run(scenario: dict[str, Any], tmp_path: Path):  # type: ignore[no-untyped-def]
    return investigate(scenario, tmp_path)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_id)
def test_status_failure_type_and_confidence(scenario: dict[str, Any], tmp_path: Path) -> None:
    expected = scenario["expected"]
    evidence, _, _ = run(scenario, tmp_path)

    assert evidence.status == expected["status"], evidence.summary
    assert evidence.failure_type.value == expected["failure_type"], evidence.summary
    if "min_confidence" in expected:
        assert evidence.confidence >= expected["min_confidence"], evidence.summary
    if "max_confidence" in expected:
        assert evidence.confidence <= expected["max_confidence"], evidence.summary


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_id)
def test_ranked_hypotheses_and_what_they_cite(scenario: dict[str, Any], tmp_path: Path) -> None:
    expected = scenario["expected"]
    evidence, _, _ = run(scenario, tmp_path)
    hypotheses = evidence.root_cause_hypotheses

    assert len(hypotheses) == len(expected["hypotheses"]), [h.hypothesis for h in hypotheses]
    for got, want in zip(hypotheses, expected["hypotheses"], strict=True):
        assert got.failure_type.value == want["failure_type"]
        for fragment in want.get("mentions") or []:
            assert str(fragment).lower() in got.hypothesis.lower(), f"{fragment!r} missing from {got.hypothesis!r}"

    top_supporting = hypotheses[0].supporting_evidence if hypotheses else []
    top_contradicting = hypotheses[0].contradicting_evidence if hypotheses else []
    assert sorted(top_supporting) == sorted(expected["supporting_evidence"])
    assert sorted(top_contradicting) == sorted(expected["contradicting_evidence"])

    known = {item.id for item in evidence.evidence_items}
    for hypothesis in hypotheses:
        assert set(hypothesis.supporting_evidence) | set(hypothesis.contradicting_evidence) <= known
    assert {item.kind for item in evidence.evidence_items} <= {"metric"}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_id)
def test_text_says_what_it_should_and_nothing_it_should_not(scenario: dict[str, Any], tmp_path: Path) -> None:
    expected = scenario["expected"]
    evidence, _, _ = run(scenario, tmp_path)
    text = " ".join([evidence.summary, *(h.hypothesis for h in evidence.root_cause_hypotheses)]).lower()
    steps = " ".join(evidence.recommended_next_steps).lower()

    for fragment in expected.get("must_not_mention") or []:
        assert str(fragment).lower() not in text, f"{fragment!r} should not appear in: {text}"
    for fragment in expected.get("next_steps_mention") or []:
        assert str(fragment).lower() in steps, f"{fragment!r} missing from next steps: {steps}"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_id)
def test_tool_calls_match_the_declared_query_plan(scenario: dict[str, Any], tmp_path: Path) -> None:
    plan = scenario["expected"]["tool_calls"]
    evidence, trail, _ = run(scenario, tmp_path)

    assert len(evidence.tool_calls) == plan["count"]
    assert [call.args["promql"] for call in evidence.tool_calls] == plan["expected_promql"]
    assert [call.ok for call in evidence.tool_calls] == plan["ok"]

    # The audit trail records the same queries, in the same order.
    audited = [record for record in trail if record.event_type == "tool_call"]
    assert [record.payload["args"]["promql"] for record in audited] == plan["expected_promql"]
    assert [record.ok for record in audited] == plan["ok"]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_id)
def test_investigation_window_matches_the_declared_offsets(scenario: dict[str, Any], tmp_path: Path) -> None:
    declared = scenario["expected"]["window"]
    evidence, _, _ = run(scenario, tmp_path)

    def offset(value: str) -> float:
        return (datetime.fromisoformat(value) - FAILURE_TIME).total_seconds()

    first_call = evidence.tool_calls[0].args
    assert offset(first_call["start"]) == declared["baseline_start"]
    assert offset(first_call["end"]) == declared["end"]

    if declared["incident_start"] is None:
        assert not evidence.evidence_items
    else:
        for item in evidence.evidence_items:
            window = json.loads(item.content)["window"]
            assert offset(window["baseline_start"]) == declared["baseline_start"]
            assert offset(window["start"]) == declared["incident_start"]
            assert offset(window["end"]) == declared["end"]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_id)
def test_findings_reach_the_audit_layer(scenario: dict[str, Any], tmp_path: Path) -> None:
    """The Week 3 joint deliverable: each agent logs its findings to the audit layer."""
    evidence, trail, _ = run(scenario, tmp_path)
    by_type = {record.event_type: record for record in trail}

    for event_type in ("agent_started", "hypothesis_formed", "evidence_written", "agent_completed"):
        assert event_type in by_type, f"{event_type} missing from the trail"
    assert by_type["hypothesis_formed"].payload["failure_type"] == evidence.failure_type.value
    assert by_type["hypothesis_formed"].payload["confidence"] == pytest.approx(evidence.confidence)
    assert len(by_type["hypothesis_formed"].payload["hypotheses"]) == len(evidence.root_cause_hypotheses)
    assert by_type["evidence_written"].payload["status"] == evidence.status
    assert by_type["agent_completed"].payload["tool_calls"] == len(evidence.tool_calls)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_id)
def test_evidence_satisfies_the_shared_contract_and_is_what_was_stored(
    scenario: dict[str, Any], tmp_path: Path
) -> None:
    evidence, _, stored = run(scenario, tmp_path)

    assert Evidence.model_validate(evidence.model_dump(mode="json")) == evidence
    assert evidence.agent == AGENT
    assert evidence.redaction_applied is True
    assert stored == evidence

    # The JSON that actually reached the store satisfies P3's canonical schema. The store wraps
    # a document with an integer `version`, which is not part of the contract.
    path = tmp_path / "evidence" / str(evidence.incident_id) / f"{AGENT}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop("version")
    validate_against_canonical_schema(document)
