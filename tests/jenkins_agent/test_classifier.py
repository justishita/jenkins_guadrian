import pytest

from agents.jenkins_agent.classifier import Hypothesis, RULES, rule_based_classify
from agents.jenkins_agent.parsing import BuildStage, CompileError, FailingTest, ParsedBuild
from common.models import FailureTaxonomy


@pytest.mark.parametrize(
    ("parsed", "failure_type"),
    [
        (
            ParsedBuild(failing_tests=[FailingTest("test_x", "test_x.py", 1, "AssertionError: mismatch")]),
            FailureTaxonomy.CODE_TEST_FAILURE,
        ),
        (
            ParsedBuild(
                failed_stage="Build",
                compiler_errors=[CompileError("app.py", 1, "SyntaxError: invalid syntax")],
            ),
            FailureTaxonomy.BUILD_COMPILATION_FAILURE,
        ),
        (
            ParsedBuild(error_blocks=["ERROR: pip dependency resolution conflict: incompatible versions"]),
            FailureTaxonomy.DEPENDENCY_REGRESSION,
        ),
        (
            ParsedBuild(
                failed_stage="Tests",
                timeouts=["Read timed out while waiting for response"],
            ),
            FailureTaxonomy.TIMEOUT,
        ),
        (
            ParsedBuild(error_blocks=["requests.exceptions.ConnectionError: Connection refused to service"]),
            FailureTaxonomy.INFRA_NETWORK_FAILURE,
        ),
        (ParsedBuild(auth_signals=["HTTP 403: authentication failed"]), FailureTaxonomy.AUTH_FAILURE),
        (
            ParsedBuild(
                failed_stage="Deploy",
                error_blocks=["deployment rejected: invalid settings.yaml configuration"],
            ),
            FailureTaxonomy.CONFIG_ERROR,
        ),
        (
            ParsedBuild(failed_stage="Deploy", error_blocks=["health check failed after release"]),
            FailureTaxonomy.DEPLOYMENT_FAILURE,
        ),
        (
            ParsedBuild(
                failing_tests=[FailingTest("test_x", "test_x.py", 1, "AssertionError")],
                error_blocks=["Previous build passed; same commit SHA"],
            ),
            FailureTaxonomy.FLAKY_TEST,
        ),
        (ParsedBuild(), FailureTaxonomy.UNKNOWN),
    ],
)
def test_rule_classifies_each_signal(parsed: ParsedBuild, failure_type: FailureTaxonomy) -> None:
    hypotheses = rule_based_classify(parsed)

    assert failure_type in {hypothesis.failure_type for hypothesis in hypotheses}
    assert all(0 <= hypothesis.rule_confidence <= 1 for hypothesis in hypotheses)
    if failure_type is FailureTaxonomy.UNKNOWN:
        assert hypotheses == [
            Hypothesis(
                FailureTaxonomy.UNKNOWN,
                "No classification rule matched the available build evidence.",
                [],
                0.2,
            )
        ]
    else:
        matched = next(h for h in hypotheses if h.failure_type is failure_type)
        assert matched.matched_evidence_ids


def test_module_not_found_can_match_compilation_and_dependency_rules() -> None:
    parsed = ParsedBuild(
        failed_stage="Build",
        compiler_errors=[CompileError("worker.py", 3, "ModuleNotFoundError: No module named 'lib'")],
    )

    hypotheses = rule_based_classify(parsed)

    assert {h.failure_type for h in hypotheses} == {
        FailureTaxonomy.BUILD_COMPILATION_FAILURE,
        FailureTaxonomy.DEPENDENCY_REGRESSION,
    }
    assert all("compiler_errors[0]" in h.matched_evidence_ids for h in hypotheses)


def test_independent_network_auth_and_test_signals_all_return() -> None:
    parsed = ParsedBuild(
        failed_stage="Tests",
        failing_tests=[FailingTest("test_api", "test_api.py", 4, "AssertionError: expected 200")],
        error_blocks=["503 Service Unavailable from dependency"],
        auth_signals=["401 authentication failed"],
    )

    hypotheses = rule_based_classify(parsed)

    assert {h.failure_type for h in hypotheses} == {
        FailureTaxonomy.CODE_TEST_FAILURE,
        FailureTaxonomy.INFRA_NETWORK_FAILURE,
        FailureTaxonomy.AUTH_FAILURE,
    }


@pytest.mark.parametrize(
    "message",
    [
        "ConnectionRefusedError: connection to dependency refused",
        "Temporary failure in name resolution for dependency.local",
        "503 Service Unavailable from dependency",
    ],
)
def test_dependency_network_signal_variants(message: str) -> None:
    hypotheses = rule_based_classify(ParsedBuild(error_blocks=[message]))

    assert [h.failure_type for h in hypotheses] == [FailureTaxonomy.INFRA_NETWORK_FAILURE]


def test_only_timeout_text_counts_as_timeout_in_test_stage() -> None:
    parsed = ParsedBuild(failed_stage="Tests", timeouts=["Connection refused"])

    assert FailureTaxonomy.TIMEOUT not in {
        hypothesis.failure_type for hypothesis in rule_based_classify(parsed)
    }


def test_non_build_compile_error_is_not_classified_as_build_failure() -> None:
    parsed = ParsedBuild(
        failed_stage="Tests",
        compiler_errors=[CompileError("app.py", 1, "SyntaxError: invalid syntax")],
    )

    assert FailureTaxonomy.BUILD_COMPILATION_FAILURE not in {
        hypothesis.failure_type for hypothesis in rule_based_classify(parsed)
    }


def test_rules_are_data_driven() -> None:
    assert RULES
    assert all(callable(rule.match) for rule in RULES)