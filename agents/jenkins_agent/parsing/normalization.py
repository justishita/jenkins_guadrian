"""Pure parsing helpers for normalized Jenkins build evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any, Pattern


ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\)|[@-_])"
)
WORKSPACE_PATH_RE = re.compile(
    r"(?i)(?:[A-Z]:[\\/]|/)[^\s:\"'<>|]*?[\\/](?:jenkins_home[\\/])?workspace[\\/][^\s:\"'<>|]*"
)
TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
    r"|\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"
)
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)
HEX_ADDRESS_RE = re.compile(r"(?<![\w])0[xX][0-9a-fA-F]+")
DURATION_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?|s|secs?|seconds?|m|mins?|minutes?|h|hours?)"
    r"(?:\s+\d+(?:\.\d+)?\s*(?:ms|milliseconds?|s|secs?|seconds?|m|mins?|minutes?|h|hours?))*\b",
    re.IGNORECASE,
)
DURATION_CLOCK_RE = re.compile(
    r"\b(?:duration|elapsed|time)\s*[:=]\s*\d{1,2}:\d{2}:\d{2}(?:\.\d+)?",
    re.IGNORECASE,
)
PID_RE = re.compile(r"\b(?:pid|process\s+id)\s*[=: ]\s*\d+\b", re.IGNORECASE)
BUILD_NUMBER_RE = re.compile(
    r"\bbuild(?:\s+number)?\s*[:#]?\s*\d+\b|#\d+\b",
    re.IGNORECASE,
)
CARRIAGE_RETURN_PROGRESS_RE = re.compile(r"[^\n]*\r")

DEFAULT_ERROR_PATTERNS = (
    r"ERROR",
    r"FAILED",
    r"Traceback",
    r"Exception",
    r"error:",
    r"fatal:",
    r"npm ERR!",
    r"BUILD FAILURE",
    r"exit code",
    r"AssertionError",
    r"SyntaxError",
    r"ModuleNotFoundError",
    r"ImportError",
    r"Timeout",
    r"Connection refused",
    r"401",
    r"403",
)

PYTEST_SUMMARY_RE = re.compile(
    r"^\s*FAILED\s+(?P<file>.+?\.py)::(?P<name>[^\s]+)(?:\s+-\s+(?P<message>.*))?$"
)
PYTEST_HEADER_RE = re.compile(r"^\s*_{3,}\s*(.*?)\s*_{3,}\s*$")
FILE_LINE_RE = re.compile(r"(?P<file>(?:[A-Za-z]:)?[^:\n]*?\.py):(?P<line>\d+)(?=[:\s])")
PYTHON_FRAME_RE = re.compile(r'^\s*File ["\'](?P<file>.+?)["\'], line (?P<line>\d+)')
PYTHON_EXCEPTION_RE = re.compile(
    r"\b(?P<kind>SyntaxError|ImportError|ModuleNotFoundError)\b(?P<message>.*)",
    re.IGNORECASE,
)
DIRECT_COMPILER_ERROR_RE = re.compile(
    r"^\s*(?P<file>(?:[A-Za-z]:)?[^:\n]+):(?P<line>\d+)(?::(?P<column>\d+))?"
    r":\s*(?:fatal\s+)?error:\s*(?P<message>.*)$",
    re.IGNORECASE,
)
ERROR_LINE_RE = re.compile(r"\b(?:fatal\s+)?error:\s*", re.IGNORECASE)
EXIT_CODE_RE = re.compile(
    r"(?:exit(?:ed)?\s+with\s+(?:code|status)|exit\s+code|exit_code)\s*[:=]?\s*(-?\d+)",
    re.IGNORECASE,
)
TIMEOUT_RE = re.compile(
    r"timeout|timed out|read timed out|connectionrefused|connection refused|connection reset",
    re.IGNORECASE,
)
AUTH_SIGNAL_RE = re.compile(
    r"\b(?:401|403)\b|permission denied|authentication failed|invalid token",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BuildStage:
    name: str
    result: str | None
    duration_ms: float | None


@dataclass(frozen=True, slots=True)
class FailingTest:
    name: str
    file: str | None
    line: int | None
    message: str


@dataclass(frozen=True, slots=True)
class CompileError:
    file: str | None
    line: int | None
    message: str
    column: int | None = None


@dataclass(slots=True)
class ParsedBuild:
    stages: list[BuildStage] = field(default_factory=list)
    failed_stage: str | None = None
    error_blocks: list[str] = field(default_factory=list)
    failing_tests: list[FailingTest] = field(default_factory=list)
    compiler_errors: list[CompileError] = field(default_factory=list)
    exit_codes: list[int] = field(default_factory=list)
    timeouts: list[str] = field(default_factory=list)
    auth_signals: list[str] = field(default_factory=list)
    error_signature: str = ""


def strip_ansi(text: str) -> str:
    """Remove ANSI CSI, OSC, and single-character escape sequences."""
    return ANSI_ESCAPE_RE.sub("", text)


def normalize_line(line: str) -> str:
    """Replace volatile Jenkins/build identifiers with stable placeholders."""
    normalized = strip_ansi(line)
    normalized = WORKSPACE_PATH_RE.sub("<PATH>", normalized)
    normalized = TIMESTAMP_RE.sub("<TS>", normalized)
    normalized = UUID_RE.sub("<UUID>", normalized)
    normalized = HEX_ADDRESS_RE.sub("<HEX>", normalized)
    normalized = DURATION_CLOCK_RE.sub("<DUR>", normalized)
    normalized = DURATION_RE.sub("<DUR>", normalized)
    normalized = PID_RE.sub("<N>", normalized)
    return BUILD_NUMBER_RE.sub("<N>", normalized)


def _compile_patterns(patterns: Sequence[str | Pattern[str]]) -> list[Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) if isinstance(pattern, str) else pattern for pattern in patterns]


def extract_error_blocks(
    text: str,
    context: int = 15,
    max_blocks: int = 8,
    patterns: Sequence[str | Pattern[str]] | None = None,
) -> list[str]:
    """Return merged line windows around matching error markers."""
    if context < 0:
        raise ValueError("context must be non-negative")
    if max_blocks < 0:
        raise ValueError("max_blocks must be non-negative")
    if max_blocks == 0:
        return []

    lines = strip_ansi(text).splitlines()
    compiled_patterns = _compile_patterns(
        DEFAULT_ERROR_PATTERNS if patterns is None else patterns
    )
    windows = [
        (max(0, index - context), min(len(lines), index + context + 1))
        for index, line in enumerate(lines)
        if any(pattern.search(line) for pattern in compiled_patterns)
    ]
    if not windows:
        return []

    merged: list[list[int]] = []
    for start, end in windows:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    selected = merged[-max_blocks:]
    return ["\n".join(lines[start:end]) for start, end in selected]


def _pytest_message(block: str, fallback: str = "") -> str:
    error_lines = [
        re.sub(r"^\s*E\s?", "", line).strip()
        for line in block.splitlines()
        if re.match(r"^\s*E(?:\s|$)", line)
    ]
    meaningful_lines = [line for line in error_lines if line]
    return "; ".join(meaningful_lines) if meaningful_lines else fallback.strip()


def parse_pytest_failures(text: str) -> list[FailingTest]:
    """Extract pytest failures from detailed sections or short-summary lines."""
    lines = strip_ansi(text).splitlines()
    summary: dict[str, tuple[str, int | None, str]] = {}
    for line in lines:
        match = PYTEST_SUMMARY_RE.match(line)
        if match:
            name = match.group("name")
            summary[name] = (match.group("file"), None, match.group("message") or "")

    headers = [
        (index, match.group(1).strip())
        for index, line in enumerate(lines)
        if (match := PYTEST_HEADER_RE.match(line))
    ]
    failures: dict[tuple[str, str | None], FailingTest] = {}
    for header_index, (start, name) in enumerate(headers):
        end = headers[header_index + 1][0] if header_index + 1 < len(headers) else len(lines)
        block = "\n".join(lines[start:end])
        locations = list(FILE_LINE_RE.finditer(block))
        file_name = locations[-1].group("file").strip() if locations else None
        line_number = int(locations[-1].group("line")) if locations else None
        summary_entry = next(
            (
                entry
                for summary_name, entry in summary.items()
                if summary_name.endswith(name) or name.endswith(summary_name)
            ),
            None,
        )
        if file_name is None and summary_entry is not None:
            file_name = summary_entry[0]
        fallback = summary_entry[2] if summary_entry is not None else ""
        message = _pytest_message(block, fallback)
        failure = FailingTest(name or "unknown", file_name, line_number, message)
        failures[(failure.name, failure.file)] = failure

    for name, (file_name, line_number, message) in summary.items():
        short_name = name.rsplit("::", 1)[-1]
        if not any(existing.name == short_name and existing.file == file_name for existing in failures.values()):
            failures[(short_name, file_name)] = FailingTest(short_name, file_name, line_number, message)

    return list(failures.values())


def parse_python_compile_errors(text: str) -> list[CompileError]:
    """Extract Python syntax/import failures and common compiler error lines."""
    errors: list[CompileError] = []
    last_file: str | None = None
    last_line: int | None = None

    for line in strip_ansi(text).splitlines():
        frame = PYTHON_FRAME_RE.match(line)
        if frame:
            last_file = frame.group("file")
            last_line = int(frame.group("line"))
            continue

        direct = DIRECT_COMPILER_ERROR_RE.match(line)
        if direct:
            errors.append(
                CompileError(
                    direct.group("file").strip(),
                    int(direct.group("line")),
                    direct.group("message").strip(),
                    int(direct.group("column")) if direct.group("column") else None,
                )
            )
            continue

        python_error = PYTHON_EXCEPTION_RE.search(line)
        if python_error:
            errors.append(
                CompileError(
                    last_file,
                    last_line,
                    f"{python_error.group('kind')}{python_error.group('message')}".strip(),
                )
            )
            last_file = None
            last_line = None
            continue

        if ERROR_LINE_RE.search(line):
            errors.append(CompileError(None, None, line.strip()))

    unique: dict[tuple[str | None, int | None, int | None, str], CompileError] = {}
    for error in errors:
        unique[(error.file, error.line, error.column, error.message)] = error
    return list(unique.values())


def parse_junit_failures(xml_text: str | bytes) -> list[FailingTest]:
    """Extract failed/error test cases from a Jenkins JUnit XML report."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    failures = []
    for testcase in root.iter():
        if testcase.tag.rsplit("}", 1)[-1] != "testcase":
            continue
        failure_node = next(
            (
                node
                for node in testcase
                if node.tag.rsplit("}", 1)[-1] in {"failure", "error"}
            ),
            None,
        )
        if failure_node is None:
            continue

        detail = "".join(failure_node.itertext()).strip()
        message = failure_node.get("message") or next(
            (line.strip() for line in detail.splitlines() if line.strip()),
            "",
        )
        file_name = testcase.get("file")
        line_text = testcase.get("line")
        frame = next(iter(PYTHON_FRAME_RE.finditer(detail)), None)
        if frame:
            file_name = file_name or frame.group("file")
            line_text = line_text or frame.group("line")
        try:
            line_number = int(line_text) if line_text else None
        except ValueError:
            line_number = None
        failures.append(
            FailingTest(
                name=testcase.get("name", "unknown"),
                file=file_name,
                line=line_number,
                message=message,
            )
        )
    return failures


def compute_error_signature(failed_stage: str | None, error_blocks: Sequence[str]) -> str:
    """Fingerprint the stage and first three normalized error lines."""
    matcher = _compile_patterns(DEFAULT_ERROR_PATTERNS)
    marked_lines = [
        line.strip()
        for block in error_blocks
        for line in strip_ansi(block).splitlines()
        if line.strip() and any(pattern.search(line) for pattern in matcher)
    ]
    candidate_lines = marked_lines or [
        line.strip()
        for block in error_blocks
        for line in strip_ansi(block).splitlines()
        if line.strip()
    ]
    normalized = [normalize_line(failed_stage or "")]
    normalized.extend(normalize_line(line) for line in candidate_lines[:3])
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def _stage_results(
    stage_summary: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
) -> list[BuildStage]:
    if stage_summary is None:
        return []
    raw_stages = (
        stage_summary.get("stages", [])
        if isinstance(stage_summary, Mapping)
        else stage_summary
    )
    stages = []
    for item in raw_stages:
        duration = item.get("durationMillis", item.get("duration"))
        try:
            duration_ms = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_ms = None
        stages.append(
            BuildStage(
                str(item.get("name", item.get("stage", "unknown"))),
                item.get("status", item.get("result")),
                duration_ms,
            )
        )
    return stages


def parse_build(
    text: str,
    stage_summary: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    failed_stage: str | None = None,
    *,
    junit_xml: str | bytes | None = None,
) -> ParsedBuild:
    """Build a typed summary from Jenkins console text and optional wfapi stages."""
    stages = _stage_results(stage_summary)
    if failed_stage is None:
        failed_stages = [
            stage.name
            for stage in stages
            if (stage.result or "").upper() in {"FAILURE", "FAILED", "ABORTED", "UNSTABLE"}
        ]
        failed_stage = failed_stages[-1] if failed_stages else None

    cleaned_text = CARRIAGE_RETURN_PROGRESS_RE.sub("", strip_ansi(text))
    lines = cleaned_text.splitlines()
    error_blocks = extract_error_blocks(cleaned_text)
    junit_failures = parse_junit_failures(junit_xml) if junit_xml is not None else []
    return ParsedBuild(
        stages=stages,
        failed_stage=failed_stage,
        error_blocks=error_blocks,
        failing_tests=junit_failures or parse_pytest_failures(cleaned_text),
        compiler_errors=parse_python_compile_errors(cleaned_text),
        exit_codes=[
            int(match.group(1))
            for line in lines
            if (match := EXIT_CODE_RE.search(line))
        ],
        timeouts=list(dict.fromkeys(line.strip() for line in lines if TIMEOUT_RE.search(line))),
        auth_signals=list(dict.fromkeys(line.strip() for line in lines if AUTH_SIGNAL_RE.search(line))),
        error_signature=compute_error_signature(failed_stage, error_blocks),
    )