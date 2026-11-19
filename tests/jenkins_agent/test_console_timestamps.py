"""Real Jenkins consoles carry a `[timestamp] ` prefix on every line (the Jenkinsfile uses `timestamps()`); parsing must not depend on it.

Found live (2026-11-19): the parser found 0 failing tests in a build whose console said `FAILED tests/test_orders.py::... - assert 3 == 4`,
because its patterns anchor at the start of the line and the fixtures had no timestamps. Every unit-test and flaky failure was then read
from lint noise. Each fixture is parsed here with and without the prefix and must give the same answer.
"""

import json
from pathlib import Path

import pytest

from agents.jenkins_agent.classifier import rule_based_classify
from agents.jenkins_agent.parsing import parse_build
from agents.jenkins_agent.parsing.normalization import clean_console, extract_error_blocks

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIOS = ("tc01_unit_test", "tc02_compile_syntax", "tc02_compile_import", "tc03_many_test_failures")


def stamp(log: str) -> str:
    """What Jenkins's timestamps() option does to a console."""
    return "\n".join(f"[2026-11-19T11:42:{n % 60:02d}.{n % 1000:03d}Z] {line}" for n, line in enumerate(log.splitlines()))


def load(name: str) -> str:
    return (FIXTURES / name / "build.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_a_timestamped_console_parses_exactly_like_a_plain_one(scenario) -> None:
    plain, stamped = load(scenario), stamp(load(scenario))
    a, b = parse_build(plain, failed_stage="Test"), parse_build(stamped, failed_stage="Test")
    assert [t.name for t in b.failing_tests] == [t.name for t in a.failing_tests]
    assert [(e.file, e.line) for e in b.compiler_errors] == [(e.file, e.line) for e in a.compiler_errors]
    assert [h.failure_type for h in rule_based_classify(b)] == [h.failure_type for h in rule_based_classify(a)]


def test_the_failing_test_in_a_timestamped_real_style_console_is_found() -> None:
    truth = json.loads((FIXTURES / "tc01_unit_test" / "ground_truth.json").read_text(encoding="utf-8"))
    parsed = parse_build(stamp(load("tc01_unit_test")), failed_stage="Test")
    assert truth["test_name"] in [t.name for t in parsed.failing_tests]
    assert rule_based_classify(parsed)[0].failure_type.value == "code_test_failure"


def test_a_pytest_summary_line_with_a_timestamp_is_parsed() -> None:
    log = (
        "[2026-11-19T11:42:56.488Z] tests/test_orders.py:33: AssertionError\n"
        "[2026-11-19T11:42:56.488Z] FAILED tests/test_orders.py::test_create_and_list_orders - assert 3 == 4\n"
        "[2026-11-19T11:42:56.488Z] 1 failed, 8 passed, 1 warning in 0.95s\n"
    )
    parsed = parse_build(log, failed_stage="Test")
    assert [(t.name, t.file) for t in parsed.failing_tests] == [("test_create_and_list_orders", "tests/test_orders.py")]
    assert "assert 3 == 4" in (parsed.failing_tests[0].message or "")


def test_clean_console_only_removes_the_prefix_not_look_alike_text() -> None:
    text = "[2026-11-19T11:42:56.488Z] real line\nnot stamped [2026-11-19T11:42:56Z] mid-line stays\n[tag] [other] keeps\n"
    cleaned = clean_console(text)
    assert cleaned.splitlines() == ["real line", "not stamped [2026-11-19T11:42:56Z] mid-line stays", "[tag] [other] keeps"]


def test_error_blocks_are_extracted_from_a_timestamped_console_too() -> None:
    blocks = extract_error_blocks(stamp(load("tc02_compile_syntax")))
    assert blocks and all("[2026-11-19T" not in block for block in blocks)
