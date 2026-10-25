"""Unit and regression tests for Jenkins agent confidence scoring."""

from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from agents.jenkins_agent.classifier import Hypothesis
from agents.jenkins_agent.confidence import (
    ConfidenceBreakdown,
    ConfidenceWeights,
    compute_confidence,
)
from agents.jenkins_agent.parsing import (
    BuildStage,
    CompileError,
    FailingTest,
    ParsedBuild,
)
from agents.jenkins_agent.tools.jenkins_client import ConsoleText
from common.models import EvidenceHypothesis, FailureTaxonomy, IncidentCreatedEvent
from agents.jenkins_agent.agent import _fallback_evidence


@pytest.fixture
def full_parsed_build() -> ParsedBuild:
    return ParsedBuild(
        stages=[
            BuildStage("Build", "SUCCESS", 2000.0),
            BuildStage("Test", "FAILED", 4500.0),
        ],
        failed_stage="Test",
        error_blocks=[
            "FAILED tests/test_orders.py::test_checkout - AssertionError: expected 200, got 500"
        ],
        failing_tests=[
            FailingTest(
                name="test_checkout",
                file="tests/test_orders.py",
                line=42,
                message="AssertionError: expected 200, got 500",
            )
        ],
        compiler_errors=[],
        exit_codes=[1],
        timeouts=[],
        auth_signals=[],
        error_signature="AssertionError: expected 200, got 500",
    )


@pytest.fixture
def full_rule_hypotheses() -> list[Hypothesis]:
    return [
        Hypothesis(
            failure_type=FailureTaxonomy.CODE_TEST_FAILURE,
            reason="A failing pytest test reports an AssertionError.",
            matched_evidence_ids=["failing_tests[0]", "stages[1]"],
            rule_confidence=0.9,
        )
    ]


@pytest.fixture
def full_llm_hypotheses() -> list[EvidenceHypothesis]:
    return [
        EvidenceHypothesis(
            hypothesis="Assertion error in checkout integration test",
            failure_type=FailureTaxonomy.CODE_TEST_FAILURE,
            confidence=0.9,
            supporting_evidence=["failing_tests[0]"],
            contradicting_evidence=[],
        )
    ]


@pytest.fixture
def clean_log_meta() -> dict[str, bool]:
    return {"truncated": False, "partial": False, "is_empty": False}


@pytest.fixture
def sample_history() -> list[dict[str, object]]:
    return [
        {"number": 40, "result": "SUCCESS"},
        {"number": 41, "result": "SUCCESS"},
        {"number": 42, "result": "FAILURE"},
    ]


def test_full_evidence_case_reaches_high_confidence(
    full_parsed_build: ParsedBuild,
    full_rule_hypotheses: list[Hypothesis],
    full_llm_hypotheses: list[EvidenceHypothesis],
    clean_log_meta: dict[str, bool],
    sample_history: list[dict[str, object]],
) -> None:
    """Full-evidence case must produce confidence >= 0.85."""
    breakdown = compute_confidence(
        parsed=full_parsed_build,
        rule_hypotheses=full_rule_hypotheses,
        llm_hypotheses=full_llm_hypotheses,
        log_meta=clean_log_meta,
        history=sample_history,
    )

    assert breakdown.total >= 0.85
    assert not breakdown.penalties
    assert breakdown.components["rule_match"] >= 0.85
    assert breakdown.components["llm_agreement"] >= 0.85
    assert breakdown.components["signal_strength"] >= 0.85
    assert breakdown.components["history_support"] >= 0.80
    assert breakdown.hypothesis_confidences[FailureTaxonomy.CODE_TEST_FAILURE] >= 0.85


def test_empty_log_caps_confidence_at_twenty_percent() -> None:
    """Empty log must result in confidence <= 0.2."""
    empty_parsed = ParsedBuild()

    breakdown_dict_meta = compute_confidence(
        parsed=empty_parsed,
        rule_hypotheses=[],
        llm_hypotheses=[],
        log_meta={"is_empty": True},
        history=[],
    )
    assert breakdown_dict_meta.total <= 0.20
    assert any("empty_log" in p for p in breakdown_dict_meta.penalties)

    # Also test empty ConsoleText
    breakdown_console_meta = compute_confidence(
        parsed=empty_parsed,
        rule_hypotheses=[],
        llm_hypotheses=[],
        log_meta=ConsoleText(text="", truncated=False, partial=False),
        history=[],
    )
    assert breakdown_console_meta.total <= 0.20
    assert any("empty_log" in p for p in breakdown_console_meta.penalties)


def test_truncated_log_confidence_lower_than_full(
    full_parsed_build: ParsedBuild,
    full_rule_hypotheses: list[Hypothesis],
    full_llm_hypotheses: list[EvidenceHypothesis],
    sample_history: list[dict[str, object]],
) -> None:
    """Truncated log must yield lower confidence than full evidence."""
    full_breakdown = compute_confidence(
        parsed=full_parsed_build,
        rule_hypotheses=full_rule_hypotheses,
        llm_hypotheses=full_llm_hypotheses,
        log_meta={"truncated": False, "partial": False},
        history=sample_history,
    )

    truncated_breakdown = compute_confidence(
        parsed=full_parsed_build,
        rule_hypotheses=full_rule_hypotheses,
        llm_hypotheses=full_llm_hypotheses,
        log_meta={"truncated": True, "partial": False},
        history=sample_history,
    )

    assert truncated_breakdown.total < full_breakdown.total
    assert any("truncated_log" in p for p in truncated_breakdown.penalties)
    assert (
        truncated_breakdown.hypothesis_confidences[FailureTaxonomy.CODE_TEST_FAILURE]
        < full_breakdown.hypothesis_confidences[FailureTaxonomy.CODE_TEST_FAILURE]
    )


def test_rules_and_llm_disagree_produces_lower_confidence(
    full_parsed_build: ParsedBuild,
    full_rule_hypotheses: list[Hypothesis],
    clean_log_meta: dict[str, bool],
    sample_history: list[dict[str, object]],
) -> None:
    """When rules and LLM disagree on failure cause, confidence must be lower."""
    agreeing_llm = [
        EvidenceHypothesis(
            hypothesis="Assertion error in checkout",
            failure_type=FailureTaxonomy.CODE_TEST_FAILURE,
            confidence=0.9,
        )
    ]
    disagreeing_llm = [
        EvidenceHypothesis(
            hypothesis="External network failure caused timeout",
            failure_type=FailureTaxonomy.INFRA_NETWORK_FAILURE,
            confidence=0.9,
        )
    ]

    agree_breakdown = compute_confidence(
        parsed=full_parsed_build,
        rule_hypotheses=full_rule_hypotheses,
        llm_hypotheses=agreeing_llm,
        log_meta=clean_log_meta,
        history=sample_history,
    )

    disagree_breakdown = compute_confidence(
        parsed=full_parsed_build,
        rule_hypotheses=full_rule_hypotheses,
        llm_hypotheses=disagreeing_llm,
        log_meta=clean_log_meta,
        history=sample_history,
    )

    assert disagree_breakdown.total < agree_breakdown.total
    assert any("rule_llm_disagreement" in p for p in disagree_breakdown.penalties)
    assert disagree_breakdown.components["llm_agreement"] < agree_breakdown.components["llm_agreement"]


def test_unknown_classification_caps_confidence_at_thirty_percent(
    clean_log_meta: dict[str, bool],
) -> None:
    """Unknown classification must cap confidence at <= 0.3."""
    vague_parsed = ParsedBuild(
        stages=[BuildStage("Test", "FAILED", 1000.0)],
        failed_stage="Test",
        error_blocks=["Process terminated with unspecified status"],
    )
    unknown_rule = [
        Hypothesis(
            failure_type=FailureTaxonomy.UNKNOWN,
            reason="No classification rule matched the available build evidence.",
            matched_evidence_ids=[],
            rule_confidence=0.2,
        )
    ]

    breakdown = compute_confidence(
        parsed=vague_parsed,
        rule_hypotheses=unknown_rule,
        llm_hypotheses=[],
        log_meta=clean_log_meta,
        history=[],
    )

    assert breakdown.total <= 0.30
    assert any("unknown_classification" in p for p in breakdown.penalties)
    assert breakdown.hypothesis_confidences[FailureTaxonomy.UNKNOWN] <= 0.30


def test_hypotheses_confidences_are_independent_and_not_forced_to_sum_to_one(
    full_parsed_build: ParsedBuild,
    clean_log_meta: dict[str, bool],
) -> None:
    """Hypotheses are independent beliefs and must NOT be normalized to sum to 1."""
    two_hypotheses = [
        Hypothesis(
            failure_type=FailureTaxonomy.CODE_TEST_FAILURE,
            reason="AssertionError in unit test",
            matched_evidence_ids=["failing_tests[0]"],
            rule_confidence=0.90,
        ),
        Hypothesis(
            failure_type=FailureTaxonomy.DEPENDENCY_REGRESSION,
            reason="Dependency resolution problem suspected",
            matched_evidence_ids=["error_blocks[0]"],
            rule_confidence=0.65,
        ),
    ]

    breakdown = compute_confidence(
        parsed=full_parsed_build,
        rule_hypotheses=two_hypotheses,
        llm_hypotheses=None,
        log_meta=clean_log_meta,
        history=None,
    )

    conf1 = breakdown.hypothesis_confidences[FailureTaxonomy.CODE_TEST_FAILURE]
    conf2 = breakdown.hypothesis_confidences[FailureTaxonomy.DEPENDENCY_REGRESSION]

    assert conf1 == pytest.approx(0.90)
    assert conf2 == pytest.approx(0.65)
    # The sum of beliefs is 1.55, explicitly demonstrating they do not sum to 1
    assert conf1 + conf2 > 1.0


def test_weights_are_configurable_via_dataclass(
    full_parsed_build: ParsedBuild,
    full_rule_hypotheses: list[Hypothesis],
    clean_log_meta: dict[str, bool],
    sample_history: list[dict[str, object]],
) -> None:
    """ConfidenceWeights can be customized to re-weight components or penalties."""
    custom_weights = ConfidenceWeights(
        w_rule=0.70,
        w_llm=0.10,
        w_signal=0.10,
        w_history=0.10,
        truncation_penalty=0.40,
    )

    breakdown_truncated = compute_confidence(
        parsed=full_parsed_build,
        rule_hypotheses=full_rule_hypotheses,
        llm_hypotheses=None,
        log_meta={"truncated": True, "partial": False},
        history=sample_history,
        weights=custom_weights,
    )

    assert any("-0.40" in p for p in breakdown_truncated.penalties)


def test_breakdown_included_in_evidence_is_auditable() -> None:
    """Evidence constructed from build analysis contains the confidence breakdown."""
    event = IncidentCreatedEvent(
        incident_id=uuid4(),
        job_name="orders/main",
        build_number=99,
        build_url="http://jenkins/job/orders/job/main/99/",
        branch="main",
        git_commit="commit-99",
        failed_stage="Tests",
        timestamp=datetime.now(timezone.utc),
    )
    parsed = ParsedBuild(
        failed_stage="Tests",
        failing_tests=[
            FailingTest("test_x", "test_x.py", 10, "AssertionError: expected True")
        ],
        error_blocks=["AssertionError: expected True"],
        error_signature="AssertionError: expected True",
    )
    hypotheses = [
        Hypothesis(
            failure_type=FailureTaxonomy.CODE_TEST_FAILURE,
            reason="AssertionError in test",
            matched_evidence_ids=["failing_tests[0]"],
            rule_confidence=0.9,
        )
    ]

    evidence = _fallback_evidence(
        event=event,
        parsed=parsed,
        hypotheses=hypotheses,
        evidence_items=[],
        tool_calls=[],
    )

    breakdown_item = next(
        (item for item in evidence.evidence_items if item.id == "confidence_breakdown"),
        None,
    )
    assert breakdown_item is not None
    payload = json.loads(breakdown_item.content)
    assert "total" in payload
    assert "components" in payload
    assert "penalties" in payload
    assert "hypothesis_confidences" in payload
    assert payload["total"] == pytest.approx(evidence.confidence)


def test_tuple_unpacking_and_lookup_accessors(
    full_parsed_build: ParsedBuild,
    full_rule_hypotheses: list[Hypothesis],
    clean_log_meta: dict[str, bool],
) -> None:
    """ConfidenceBreakdown supports tuple unpacking and both string/enum lookups."""
    breakdown = compute_confidence(
        parsed=full_parsed_build,
        rule_hypotheses=full_rule_hypotheses,
        llm_hypotheses=None,
        log_meta=clean_log_meta,
        history=[],
    )

    total, components, penalties = breakdown
    assert total == breakdown.total
    assert components == breakdown.components
    assert penalties == breakdown.penalties

    # Check lookup by enum and by string
    assert (
        breakdown.hypothesis_confidences[FailureTaxonomy.CODE_TEST_FAILURE]
        == breakdown.hypothesis_confidences["code_test_failure"]
    )
    assert breakdown.per_hypothesis == breakdown.hypothesis_confidences
