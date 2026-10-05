"""Rule-based classification of normalized Jenkins build evidence."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable

from agents.jenkins_agent.parsing import ParsedBuild
from common.models import FailureTaxonomy


@dataclass(frozen=True, slots=True)
class Hypothesis:
    failure_type: FailureTaxonomy
    reason: str
    matched_evidence_ids: list[str]
    rule_confidence: float


EvidenceMatcher = Callable[[ParsedBuild], list[str]]


@dataclass(frozen=True, slots=True)
class Rule:
    failure_type: FailureTaxonomy
    reason: str
    confidence: float
    match: EvidenceMatcher


FAILED_RESULTS = {"FAILURE", "FAILED", "ABORTED", "UNSTABLE"}
STAGE_NAMES = {
    "build": {"build", "compile", "compilation"},
    "test": {"test", "tests"},
    "deploy": {"deploy", "deployment"},
}

ASSERTION_RE = re.compile(r"\bAssertionError\b", re.IGNORECASE)
COMPILATION_RE = re.compile(
    r"\b(?:SyntaxError|ImportError|ModuleNotFoundError)\b|\b(?:fatal\s+)?error:",
    re.IGNORECASE,
)
DEPENDENCY_RE = re.compile(
    r"\bModuleNotFoundError\b|(?:pip|python\s+-m\s+pip).*"
    r"(?:resolution|resolve|conflict|incompatible|version)|"
    r"(?:resolution|dependency|version).*(?:conflict|incompatible|failed)",
    re.IGNORECASE,
)
TIMEOUT_RE = re.compile(r"\btimeout\b|\btimed out\b", re.IGNORECASE)
NETWORK_RE = re.compile(
    r"connection\s*refused|name resolution|name or service not known|"
    r"temporary failure in name resolution|\b503\b|"
    r"git checkout failed|repository(?: .+)? not found|could not resolve host|"
    r"failed to connect to .*git",
    re.IGNORECASE,
)
CONFIG_RE = re.compile(
    r"config(?:uration)?|settings\.ya?ml|deploy\.env|missing environment variable|"
    r"environment variable .* (?:missing|required)|invalid yaml|malformed yaml",
    re.IGNORECASE,
)
FLAKY_HISTORY_RE = re.compile(
    r"(?:previous|prior)\s+(?:build|run|execution).*\bpassed\b.*"
    r"(?:same commit|commit unchanged|same revision)|"
    r"\bpassed\b.*(?:same commit|commit unchanged|same revision)",
    re.IGNORECASE,
)
PYTEST_COLLECTION_RE = re.compile(
    r"^\s*(?:ERROR\s+collecting\s+|ImportError while importing test module)",
    re.IGNORECASE | re.MULTILINE,
)


def _failed_stage_ids(parsed: ParsedBuild, category: str) -> list[str]:
    allowed_names = STAGE_NAMES[category]
    evidence_ids = [
        f"stages[{index}]"
        for index, stage in enumerate(parsed.stages)
        if stage.name.strip().casefold() in allowed_names
        and (stage.result or "").upper() in FAILED_RESULTS
    ]
    if parsed.failed_stage and parsed.failed_stage.strip().casefold() in allowed_names:
        evidence_ids.append("failed_stage")
    return list(dict.fromkeys(evidence_ids))


def _compiler_errors(parsed: ParsedBuild) -> list[str]:
    return [
        f"compiler_errors[{index}]"
        for index, error in enumerate(parsed.compiler_errors)
        if COMPILATION_RE.search(error.message)
    ]


def _dependency_evidence(parsed: ParsedBuild) -> list[str]:
    evidence_ids = [
        f"compiler_errors[{index}]"
        for index, error in enumerate(parsed.compiler_errors)
        if re.search(r"\bModuleNotFoundError\b", error.message, re.IGNORECASE)
    ]
    evidence_ids.extend(
        f"error_blocks[{index}]"
        for index, block in enumerate(parsed.error_blocks)
        if DEPENDENCY_RE.search(block)
    )
    return list(dict.fromkeys(evidence_ids))


def _network_evidence(parsed: ParsedBuild) -> list[str]:
    return [
        f"error_blocks[{index}]"
        for index, block in enumerate(parsed.error_blocks)
        if NETWORK_RE.search(block)
    ]


def _config_evidence(parsed: ParsedBuild) -> list[str]:
    return [
        f"error_blocks[{index}]"
        for index, block in enumerate(parsed.error_blocks)
        if CONFIG_RE.search(block)
    ]


def _test_failure_ids(parsed: ParsedBuild) -> list[str]:
    return [
        f"failing_tests[{index}]"
        for index, test in enumerate(parsed.failing_tests)
        if ASSERTION_RE.search(test.message)
    ]


def _timeout_evidence(parsed: ParsedBuild) -> list[str]:
    return [
        f"timeouts[{index}]"
        for index, signal in enumerate(parsed.timeouts)
        if TIMEOUT_RE.search(signal)
    ]


def _auth_evidence(parsed: ParsedBuild) -> list[str]:
    return [f"auth_signals[{index}]" for index in range(len(parsed.auth_signals))]


def _flaky_evidence(parsed: ParsedBuild) -> list[str]:
    if not parsed.failing_tests:
        return []
    evidence_ids = [
        f"error_blocks[{index}]"
        for index, block in enumerate(parsed.error_blocks)
        if FLAKY_HISTORY_RE.search(block)
    ]
    if evidence_ids:
        evidence_ids.extend(f"failing_tests[{index}]" for index in range(len(parsed.failing_tests)))
    return evidence_ids


def _build_compilation_evidence(parsed: ParsedBuild) -> list[str]:
    compiler_ids = _compiler_errors(parsed)
    build_stage_ids = _failed_stage_ids(parsed, "build")
    if build_stage_ids and compiler_ids:
        return [*build_stage_ids, *compiler_ids]
    if compiler_ids and any(PYTEST_COLLECTION_RE.search(block) for block in parsed.error_blocks):
        collection_ids = [
            f"error_blocks[{index}]"
            for index, block in enumerate(parsed.error_blocks)
            if PYTEST_COLLECTION_RE.search(block)
        ]
        return [*collection_ids, *compiler_ids]
    return []


def _test_timeout_evidence(parsed: ParsedBuild) -> list[str]:
    stage_ids = _failed_stage_ids(parsed, "test")
    timeout_ids = _timeout_evidence(parsed)
    return [*stage_ids, *timeout_ids] if stage_ids and timeout_ids else []


def _deployment_evidence(parsed: ParsedBuild) -> list[str]:
    stage_ids = _failed_stage_ids(parsed, "deploy")
    if not stage_ids:
        return []
    return [*stage_ids, *[f"error_blocks[{index}]" for index in range(len(parsed.error_blocks))]]


def _deployment_without_config(parsed: ParsedBuild) -> list[str]:
    return [] if _config_evidence(parsed) else _deployment_evidence(parsed)


RULES = [
    Rule(
        FailureTaxonomy.CODE_TEST_FAILURE,
        "A failing pytest test reports an AssertionError.",
        0.9,
        _test_failure_ids,
    ),
    Rule(
        FailureTaxonomy.BUILD_COMPILATION_FAILURE,
        "The failed Build stage contains a syntax, import, or compiler error.",
        0.9,
        _build_compilation_evidence,
    ),
    Rule(
        FailureTaxonomy.DEPENDENCY_REGRESSION,
        "Dependency resolution or import evidence suggests a dependency regression; confirm the change in GitHub.",
        0.65,
        _dependency_evidence,
    ),
    Rule(
        FailureTaxonomy.TIMEOUT,
        "A timeout signal occurred in the failed Test stage.",
        0.85,
        _test_timeout_evidence,
    ),
    Rule(
        FailureTaxonomy.INFRA_NETWORK_FAILURE,
        "A dependency connection, name-resolution, or service-unavailable failure was reported.",
        0.85,
        _network_evidence,
    ),
    Rule(
        FailureTaxonomy.AUTH_FAILURE,
        "The build reported an authentication or authorization failure.",
        0.9,
        _auth_evidence,
    ),
    Rule(
        FailureTaxonomy.CONFIG_ERROR,
        "The failed deployment stage reports a configuration problem.",
        0.85,
        lambda parsed: (
            [*_failed_stage_ids(parsed, "deploy"), *_config_evidence(parsed)]
            if _failed_stage_ids(parsed, "deploy") and _config_evidence(parsed)
            else []
        ),
    ),
    Rule(
        FailureTaxonomy.DEPLOYMENT_FAILURE,
        "The deployment stage failed without a specific configuration signal.",
        0.8,
        _deployment_without_config,
    ),
    Rule(
        FailureTaxonomy.FLAKY_TEST,
        "The failing test previously passed on the same commit; treat this as a flaky-test candidate.",
        0.6,
        _flaky_evidence,
    ),
]


def rule_based_classify(parsed: ParsedBuild) -> list[Hypothesis]:
    """Return every rule supported by parsed build evidence, or a low-confidence unknown."""
    hypotheses = [
        Hypothesis(rule.failure_type, rule.reason, evidence_ids, rule.confidence)
        for rule in RULES
        if (evidence_ids := rule.match(parsed))
    ]
    if hypotheses:
        return hypotheses
    return [
        Hypothesis(
            FailureTaxonomy.UNKNOWN,
            "No classification rule matched the available build evidence.",
            [],
            0.2,
        )
    ]