"""Rules and parser against REAL Jenkins consoles (saved from the live evaluation, 2026-11-19), not hand-trimmed ones.

The older fixtures were clean pytest output, and every one of these bugs hid behind that: a real console has per-line timestamps, the pip
install trace, the git plugin's `> git ... # timeout=10` echoes and a pytest `-q` summary that says `- assert 3 == 4`. Found live: the rules
called every unit-test failure a dependency regression (0.65) or a timeout (0.85) and never a code_test_failure, so the agent's correct
LLM answer was penalised for "disagreeing" with them and its confidence fell to 0.34, below the Coordinator's floor.
"""

import json
from pathlib import Path
import re

import pytest

from agents.jenkins_agent.classifier import ASSERTION_RE, rule_based_classify
from agents.jenkins_agent.parsing import parse_build
from common.models import FailureTaxonomy

FIXTURES = Path(__file__).parent / "fixtures"
REAL = ("real_tc01_unit_test", "real_tc02_compile_syntax", "real_tc02_compile_import")


def load(name: str):
    text = (FIXTURES / name / "build.log").read_text(encoding="utf-8")
    truth = json.loads((FIXTURES / name / "ground_truth.json").read_text(encoding="utf-8"))
    return text, truth


@pytest.mark.parametrize("name", REAL)
def test_the_real_console_has_the_noise_that_hid_the_bugs(name) -> None:
    text, _ = load(name)
    assert re.search(r"^\[2026-11-19T[\d:.]+Z\] ", text, re.MULTILINE)  # per-line timestamps
    assert "--disable-pip-version-check" in text  # the pipeline's own pip command
    assert re.search(r"> git .* # timeout=\d+", text)  # the git plugin's echoes


@pytest.mark.parametrize("name", REAL)
def test_the_top_rule_hypothesis_on_a_real_console_matches_the_known_fault(name) -> None:
    text, truth = load(name)
    parsed = parse_build(text, failed_stage="Test")
    hypotheses = rule_based_classify(parsed)
    accepted = {truth["expected_failure_type"], *truth.get("also_accepted", [])}
    assert hypotheses[0].failure_type.value in accepted, [(h.failure_type.value, h.rule_confidence) for h in hypotheses]


def test_a_real_unit_test_failure_is_not_also_called_a_timeout_or_a_dependency_regression() -> None:
    text, _ = load("real_tc01_unit_test")
    types = {h.failure_type for h in rule_based_classify(parse_build(text, failed_stage="Test"))}
    assert FailureTaxonomy.CODE_TEST_FAILURE in types
    assert FailureTaxonomy.TIMEOUT not in types and FailureTaxonomy.DEPENDENCY_REGRESSION not in types


def test_the_failing_test_is_attributed_to_the_test_file_not_to_a_library_frame() -> None:
    text, truth = load("real_tc01_unit_test")
    tests = parse_build(text, failed_stage="Test").failing_tests
    assert [(t.name, t.file, t.line) for t in tests] == [(truth["test_name"], truth["file"], truth["line"])]  # one entry, the right place
    assert "site-packages" not in (tests[0].file or "")


def test_git_and_pipeline_boilerplate_is_not_a_timeout_signal() -> None:
    text, _ = load("real_tc01_unit_test")
    assert parse_build(text, failed_stage="Test").timeouts == []


@pytest.mark.parametrize("message", ["assert 3 == 4", "assert False", "AssertionError: expected 3", "assert order['item'] == 'widget'"])
def test_assertion_messages_in_the_shapes_pytest_prints_are_recognised(message) -> None:
    assert ASSERTION_RE.search(message)


@pytest.mark.parametrize("message", ["TypeError: unsupported operand", "KeyError: 'id'", "reassert the claim", "the assertion library"])
def test_other_messages_are_not_taken_for_assertion_failures(message) -> None:
    assert not ASSERTION_RE.search(message)


def test_a_genuine_dependency_failure_is_still_a_dependency_failure() -> None:
    """The fix removes the pipeline's pip COMMAND from the evidence, not real pip errors."""
    log = (
        "[2026-11-19T11:00:00.000Z] + .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt\n"
        "[2026-11-19T11:00:01.000Z] ERROR: Could not find a version that satisfies the requirement httpx==0.99.0\n"
        "[2026-11-19T11:00:01.000Z] ModuleNotFoundError: No module named 'httpx'\n"
    )
    types = {h.failure_type for h in rule_based_classify(parse_build(log, failed_stage="Setup"))}
    assert FailureTaxonomy.DEPENDENCY_REGRESSION in types


def test_a_pin_to_a_version_that_does_not_exist_is_a_dependency_regression_on_pips_own_words() -> None:
    """Found on the live Azure run (TC-03): the real console has no ModuleNotFoundError line, only pip's two ERROR lines."""
    log = (
        "[2026-11-19T17:46:30.000Z] + .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt\n"
        "[2026-11-19T17:46:31.000Z] ERROR: Ignored the following yanked versions: 0.17.0\n"
        "[2026-11-19T17:46:31.000Z] ERROR: Could not find a version that satisfies the requirement uvicorn==0.99.0 (from versions: 0.0.1, 0.34.0)\n"
        "[2026-11-19T17:46:31.000Z] ERROR: No matching distribution found for uvicorn==0.99.0\n"
        "[2026-11-19T17:46:32.000Z] ERROR: script returned exit code 1\n"
    )
    hypotheses = rule_based_classify(parse_build(log, failed_stage="Setup"))
    assert hypotheses, "no rule hypothesis at all"
    assert hypotheses[0].failure_type == FailureTaxonomy.DEPENDENCY_REGRESSION


def test_a_real_timeout_message_still_counts() -> None:
    log = "[2026-11-19T11:00:00.000Z] FAILED tests/test_x.py::test_slow - Failed: Timeout >600.0s\n[2026-11-19T11:00:01.000Z] ReadTimeout: read timed out\n"
    assert parse_build(log, failed_stage="Test").timeouts
