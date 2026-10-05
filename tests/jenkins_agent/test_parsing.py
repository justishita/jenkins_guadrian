from pathlib import Path

import pytest

from agents.jenkins_agent.parsing import (
    BuildStage,
    CompileError,
    FailingTest,
    compute_error_signature,
    extract_error_blocks,
    normalize_line,
    parse_build,
    parse_junit_failures,
    parse_pytest_failures,
    parse_python_compile_errors,
    strip_ansi,
)


FIXTURES = Path(__file__).parent / "fixtures"


def read_log(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_strip_ansi_from_fixture() -> None:
    escaped = read_log("ansi_progress.log")
    text = escaped.encode("ascii").decode("unicode_escape")

    assert strip_ansi(text).startswith("ERROR\n")
    assert "\x1b" not in strip_ansi(text)


def test_normalize_line_replaces_volatile_build_values() -> None:
    line = (
        "2026-10-05T12:34:56Z /var/jenkins_home/workspace/demo/app.py "
        "build #481 pid=912 took 1.7 seconds "
        "123e4567-e89b-42d3-a456-426614174000 address=0x7ffeedcc"
    )

    normalized = normalize_line(line)

    for placeholder in ("<TS>", "<PATH>", "<N>", "<DUR>", "<UUID>", "<HEX>"):
        assert placeholder in normalized


def test_extract_error_blocks_merges_overlapping_windows_and_accepts_patterns() -> None:
    lines = [f"line {index}" for index in range(12)]
    lines[4] = "ERROR first marker"
    lines[6] = "FAILED second marker"
    text = "\n".join(lines)

    blocks = extract_error_blocks(text, context=2)
    custom = extract_error_blocks(text, context=0, patterns=[r"second marker"])
    disabled = extract_error_blocks(text, patterns=[])

    assert len(blocks) == 1
    assert "line 2" in blocks[0]
    assert "line 8" in blocks[0]
    assert len(custom) == 1
    assert custom[0] == "FAILED second marker"
    assert disabled == []


def test_extract_error_blocks_caps_to_most_recent_windows() -> None:
    text = "ERROR first\nquiet\nFAILED second\nquiet\nERROR third"

    blocks = extract_error_blocks(text, context=0, max_blocks=2)

    assert blocks == ["FAILED second", "ERROR third"]


def test_parse_pytest_failure_from_detailed_fixture() -> None:
    failures = parse_pytest_failures(read_log("pytest_assertion_failure.log"))

    assert failures == [
        FailingTest(
            name="test_rejects_invalid_branch",
            file="tests/test_checkout.py",
            line=42,
            message="assert 500 == 400",
        )
    ]


def test_parse_pytest_failure_from_summary_only() -> None:
    failures = parse_pytest_failures(
        "FAILED tests/test_api.py::test_denied - AssertionError: expected 403"
    )

    assert failures == [
        FailingTest(
            name="test_denied",
            file="tests/test_api.py",
            line=None,
            message="AssertionError: expected 403",
        )
    ]


def test_junit_failures_take_precedence_over_console_pytest_fallback() -> None:
    junit_xml = read_log("junit_report.xml")
    console = "FAILED tests/test_fallback.py::test_console - AssertionError: fallback"

    junit_failures = parse_junit_failures(junit_xml)
    parsed = parse_build(console, junit_xml=junit_xml)

    assert junit_failures == [
        FailingTest(
            name="test_create_order",
            file="tests/test_orders.py",
            line=28,
            message="AssertionError: expected status 201",
        )
    ]
    assert parsed.failing_tests == junit_failures


@pytest.mark.parametrize(
    ("fixture", "file", "line", "kind"),
    [
        ("python_syntax_error.log", "/var/jenkins_home/workspace/payments/app/main.py", 17, "SyntaxError"),
        ("missing_module.log", "/var/jenkins_home/workspace/payments/app/worker.py", 3, "ModuleNotFoundError"),
    ],
)
def test_parse_python_compile_errors_from_tracebacks(
    fixture: str,
    file: str,
    line: int,
    kind: str,
) -> None:
    errors = parse_python_compile_errors(read_log(fixture))

    assert errors == [CompileError(file=file, line=line, message=next(error.message for error in errors))]
    assert kind in errors[0].message


def test_parse_generic_compiler_error_includes_column() -> None:
    errors = parse_python_compile_errors("src/module.c:18:6: error: expected expression")

    assert errors == [
        CompileError(
            file="src/module.c",
            line=18,
            message="expected expression",
            column=6,
        )
    ]


def test_java_stack_trace_does_not_become_python_compile_error() -> None:
    assert parse_python_compile_errors(read_log("java_stacktrace.log")) == []


def test_empty_and_binary_garbage_logs_are_safe() -> None:
    assert parse_build(read_log("empty.log")).error_blocks == []

    escaped = read_log("binary_garbage.log").strip().encode("ascii")
    raw_bytes = escaped.replace(b"\\x00", b"\x00").replace(b"\\xff", b"\xff")
    decoded = raw_bytes.decode("utf-8", errors="replace")
    parsed = parse_build(decoded)

    assert "\ufffd" in decoded
    assert len(parsed.error_signature) == 64


def test_parse_build_collects_stages_exit_timeouts_and_auth_signals() -> None:
    log = "\n".join(
        [
            read_log("timeout.log"),
            read_log("auth_401.log"),
            "script returned exit code 124",
        ]
    )
    parsed = parse_build(
        log,
        stage_summary={
            "stages": [
                {"name": "Build", "status": "SUCCESS", "durationMillis": 1200},
                {"name": "Tests", "status": "FAILED", "durationMillis": 2500},
            ]
        },
    )

    assert parsed.stages == [
        BuildStage("Build", "SUCCESS", 1200.0),
        BuildStage("Tests", "FAILED", 2500.0),
    ]
    assert parsed.failed_stage == "Tests"
    assert parsed.exit_codes == [124]
    assert any("Read timed out" in signal for signal in parsed.timeouts)
    assert any("401" in signal for signal in parsed.auth_signals)
    assert any("Permission denied" in signal for signal in parsed.auth_signals)


def test_error_signature_is_stable_across_build_numbers_paths_and_times() -> None:
    first = [
        "2026-10-01T10:11:12Z ERROR build #11 pid=301 at /var/jenkins_home/workspace/a/src.py",
        "ModuleNotFoundError: missing dependency 0x7fff",
    ]
    second = [
        "2026-10-02T21:22:33Z ERROR build #98 pid=888 at /var/jenkins_home/workspace/b/src.py",
        "ModuleNotFoundError: missing dependency 0x9abc",
    ]

    assert compute_error_signature("Tests", first) == compute_error_signature("Tests", second)


def test_ten_thousand_line_log_keeps_error_at_tail() -> None:
    tail = read_log("large_log_tail.log").splitlines()
    text = "\n".join([*("quiet build output" for _ in range(9_998)), *tail])

    blocks = extract_error_blocks(text)

    assert len(text.splitlines()) == 10_000
    assert len(blocks) == 1
    assert "ordinary final context line" in blocks[0]
    assert "ERROR: failure on the final line" in blocks[0]
    assert "quiet build output" in blocks[0]