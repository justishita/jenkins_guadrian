"""Regression tests for labeled Jenkins failure scenarios and edge cases."""

import json
from pathlib import Path

import pytest

from agents.jenkins_agent.agent import _fallback_evidence, _initial_evidence_items
from agents.jenkins_agent.classifier import rule_based_classify
from agents.jenkins_agent.parsing import parse_build
from common.models import FailureTaxonomy, IncidentCreatedEvent


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
SCENARIOS = (
    "tc01_unit_test",
    "tc02_compile_syntax",
    "tc02_compile_import",
    "tc03_many_test_failures",
)


def _load_scenario(name: str) -> tuple[str, dict[str, object]]:
    directory = FIXTURE_ROOT / name
    log = (directory / "build.log").read_text(encoding="utf-8")
    truth = json.loads((directory / "ground_truth.json").read_text(encoding="utf-8"))
    return log, truth


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_scenario_classification_matches_ground_truth(scenario: str) -> None:
    log, truth = _load_scenario(scenario)
    parsed = parse_build(log, failed_stage="Test")
    hypotheses = rule_based_classify(parsed)

    assert hypotheses
    assert hypotheses[0].failure_type.value == truth["expected_failure_type"]
    assert hypotheses[0].rule_confidence >= 0.7
    if "file" in truth:
        observed_files = [
            error.file for error in parsed.compiler_errors if error.file
        ] + [test.file for test in parsed.failing_tests if test.file]
        assert any(str(truth["file"]) in observed for observed in observed_files)
        observed_lines = [
            error.line for error in parsed.compiler_errors if error.line is not None
        ] + [test.line for test in parsed.failing_tests if test.line is not None]
        assert int(truth["line"]) in observed_lines
    if "test_name" in truth:
        assert any(test.name == truth["test_name"] for test in parsed.failing_tests)


@pytest.mark.parametrize(
    ("scenario", "failure_type"),
    [
        ("git_checkout_network", FailureTaxonomy.INFRA_NETWORK_FAILURE),
        ("git_checkout_auth", FailureTaxonomy.AUTH_FAILURE),
    ],
)
def test_checkout_failures_do_not_become_test_failures(
    scenario: str,
    failure_type: FailureTaxonomy,
) -> None:
    log, _ = _load_scenario(scenario)
    parsed = parse_build(log, failed_stage="Checkout")
    hypotheses = rule_based_classify(parsed)

    assert hypotheses[0].failure_type is failure_type
    assert all(item.failure_type is not FailureTaxonomy.CODE_TEST_FAILURE for item in hypotheses)
    assert parsed.failing_tests == []


def test_collection_import_error_is_compilation_failure_not_test_failure() -> None:
    log, truth = _load_scenario("tc02_compile_import")
    parsed = parse_build(log, failed_stage="Test")
    hypotheses = rule_based_classify(parsed)

    assert parsed.failing_tests == []
    assert parsed.compiler_errors[0].line == truth["line"]
    assert hypotheses[0].failure_type is FailureTaxonomy.BUILD_COMPILATION_FAILURE
    assert all(item.failure_type is not FailureTaxonomy.CODE_TEST_FAILURE for item in hypotheses)


def test_mixed_test_and_import_errors_keep_test_failure_as_primary() -> None:
    log = (
        "FAILED tests/test_sample.py::test_alpha - AssertionError\n"
        'File "main.py", line 9\n'
        "ModuleNotFoundError: No module named x\n"
    )
    hypotheses = rule_based_classify(parse_build(log, failed_stage="Test"))

    assert hypotheses[0].failure_type is FailureTaxonomy.CODE_TEST_FAILURE
    assert any(item.failure_type is FailureTaxonomy.DEPENDENCY_REGRESSION for item in hypotheses)


def test_truncated_log_without_diagnostic_evidence_remains_unknown() -> None:
    parsed = parse_build("build output ... [truncated before the failure]")
    hypotheses = rule_based_classify(parsed)

    assert hypotheses[0].failure_type is FailureTaxonomy.UNKNOWN
    assert hypotheses[0].rule_confidence < 0.7


def test_truncated_diagnostics_reduce_fallback_confidence() -> None:
    log, _ = _load_scenario("tc01_unit_test")
    parsed = parse_build(log, failed_stage="Test")
    hypotheses = rule_based_classify(parsed)
    event = IncidentCreatedEvent(
        incident_id="11111111-2222-3333-4444-555555555555",
        job_name="target-app/main",
        build_number=42,
        build_url="http://jenkins/job/target-app/job/main/42/",
        branch="main",
        git_commit="fixture-commit",
        failed_stage="Test",
        timestamp="2026-01-01T00:00:00Z",
    )
    items = _initial_evidence_items(event, parsed, {}, None, [])

    complete = _fallback_evidence(event, parsed, hypotheses, items, [])
    truncated = _fallback_evidence(
        event,
        parsed,
        hypotheses,
        items,
        [],
        log_truncated=True,
    )

    assert truncated.confidence < complete.confidence
    assert truncated.log_truncated is True


def test_many_test_failures_are_summarized_to_top_five() -> None:
    log, truth = _load_scenario("tc03_many_test_failures")
    parsed = parse_build(log, failed_stage="Test")
    event = IncidentCreatedEvent(
        incident_id="11111111-2222-3333-4444-555555555555",
        job_name="target-app/main",
        build_number=42,
        build_url="http://jenkins/job/target-app/job/main/42/",
        branch="main",
        git_commit="fixture-commit",
        failed_stage="Test",
        timestamp="2026-01-01T00:00:00Z",
    )
    items = _initial_evidence_items(event, parsed, {}, None, [])

    assert len(parsed.failing_tests) == truth["failing_test_count"]
    assert len([item for item in items if item.id.startswith("failing_tests[")]) == 5
    assert parsed.failing_tests[0].name == "test_case_00"
    assert parsed.failing_tests[4].name == "test_case_04"


def test_crlf_console_is_normalized_before_classification() -> None:
    log, truth = _load_scenario("tc01_unit_test")
    parsed = parse_build(log.replace("\n", "\r\n"), failed_stage="Test")

    assert rule_based_classify(parsed)[0].failure_type is FailureTaxonomy.CODE_TEST_FAILURE
    assert parsed.failing_tests[0].name == truth["test_name"]
