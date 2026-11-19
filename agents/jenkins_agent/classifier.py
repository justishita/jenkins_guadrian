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

# pytest -q prints `FAILED tests/x.py::test - assert 3 == 4`: the message starts with `assert`, and the word AssertionError is on another
# line of the report, so requiring the word missed every real assertion failure.
ASSERTION_RE = re.compile(r"\bAssertionError\b|^\s*assert\b", re.IGNORECASE | re.MULTILINE)
COMPILATION_RE = re.compile(
    r"\b(?:SyntaxError|ImportError|ModuleNotFoundError)\b|\b(?:fatal\s+)?error:",
    re.IGNORECASE,
)
# BUILD_COMPILATION_FAILURE only fires when a compiler_error has a Python exception
# keyword OR a file+line reference.  Bare 'ERROR: …' lines from pip/tools are
# excluded so they don't shadow DEPENDENCY_REGRESSION.
PYTHON_COMPILE_SPECIFIC_RE = re.compile(
    r"\b(?:SyntaxError|ImportError|ModuleNotFoundError|SyntaxWarning)\b|"
    r"File \"[^\"]+\", line \d+",
    re.IGNORECASE,
)
DEPENDENCY_RE = re.compile(
    r"\bModuleNotFoundError\b|"
    # pip's own words for a pin that does not exist or cannot be resolved: neither line contains the word "pip"
    r"Could not find a version that satisfies the requirement|No matching distribution found|ResolutionImpossible|"
    r"(?:pip|python\s+-m\s+pip).*"
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
# A real pytest report headlines a collection error with an underscore rule (`____ ERROR collecting x.py ____`), then repeats it in the short
# summary (`ERROR tests/x.py`) and ends with `Interrupted: 1 error during collection`. Only a bare `ERROR collecting` line was recognised,
# so a SyntaxError raised while pytest imported the app was classified `unknown` (0.2) in a real build.
PYTEST_COLLECTION_RE = re.compile(
    r"^\s*(?:_{3,}\s*)?ERROR\s+collecting\s+|ImportError while importing test module|"
    r"Interrupted:\s*\d+\s+errors?\s+during\s+collection|^ERROR\s+\S+\.py\b",
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
    # Only count errors that have Python exception keywords or file+line info.
    # This prevents bare 'ERROR: pip ...' lines from triggering BUILD_COMPILATION_FAILURE.
    return [
        f"compiler_errors[{index}]"
        for index, error in enumerate(parsed.compiler_errors)
        if PYTHON_COMPILE_SPECIFIC_RE.search(error.message)
        or (error.file is not None and error.line is not None)
    ]


#: The shell trace of the pipeline's own install step (`+ .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt`)
#: matched DEPENDENCY_RE's `pip ... version` in every build and made every failure look like a dependency regression (0.65).
PIP_COMMAND_NOISE_RE = re.compile(r"--disable-pip-version-check|^\s*(?:\[[^\]]*\]\s*)?\+\s.*$", re.MULTILINE)


def _dependency_evidence(parsed: ParsedBuild) -> list[str]:
    evidence_ids = [
        f"compiler_errors[{index}]"
        for index, error in enumerate(parsed.compiler_errors)
        if re.search(r"\bModuleNotFoundError\b", error.message, re.IGNORECASE)
    ]
    evidence_ids.extend(
        f"error_blocks[{index}]"
        for index, block in enumerate(parsed.error_blocks)
        if DEPENDENCY_RE.search(PIP_COMMAND_NOISE_RE.sub('', block))
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


def rule_based_classify(
    parsed: ParsedBuild,
    flaky_score: float | None = None,
) -> list[Hypothesis]:
    """Return every rule supported by parsed build evidence, adjusted for flakiness if present."""
    hypotheses = [
        Hypothesis(rule.failure_type, rule.reason, evidence_ids, rule.confidence)
        for rule in RULES
        if (evidence_ids := rule.match(parsed))
    ]

    if flaky_score is not None and flaky_score > 0.4:
        adjusted_hypotheses: list[Hypothesis] = []
        has_flaky_hyp = False
        evidence_ids = [f"failing_tests[{i}]" for i in range(len(parsed.failing_tests))]
        if not evidence_ids and parsed.error_blocks:
            evidence_ids = [f"error_blocks[{i}]" for i in range(len(parsed.error_blocks))]

        for hyp in hypotheses:
            if hyp.failure_type == FailureTaxonomy.CODE_TEST_FAILURE:
                # REDUCE code_test_failure confidence
                reduced_conf = round(max(0.15, hyp.rule_confidence * (1.0 - flaky_score)), 4)
                adjusted_hypotheses.append(
                    Hypothesis(
                        failure_type=hyp.failure_type,
                        reason=hyp.reason + " (reduced: test exhibited historical flakiness)",
                        matched_evidence_ids=hyp.matched_evidence_ids,
                        rule_confidence=reduced_conf,
                    )
                )
            elif hyp.failure_type == FailureTaxonomy.FLAKY_TEST:
                has_flaky_hyp = True
                flaky_conf = round(min(0.95, max(0.70, 0.50 + flaky_score * 0.45)), 4)
                adjusted_hypotheses.append(
                    Hypothesis(
                        failure_type=hyp.failure_type,
                        reason=(
                            f"Historical test flakiness detected (flaky_score={flaky_score:.2f}); "
                            "retry / quarantine and investigate."
                        ),
                        matched_evidence_ids=hyp.matched_evidence_ids or evidence_ids,
                        rule_confidence=flaky_conf,
                    )
                )
            else:
                adjusted_hypotheses.append(hyp)

        if not has_flaky_hyp:
            flaky_conf = round(min(0.95, max(0.70, 0.50 + flaky_score * 0.45)), 4)
            adjusted_hypotheses.insert(
                0,
                Hypothesis(
                    failure_type=FailureTaxonomy.FLAKY_TEST,
                    reason=(
                        f"Historical test flakiness detected (flaky_score={flaky_score:.2f}); "
                        "retry / quarantine and investigate."
                    ),
                    matched_evidence_ids=evidence_ids,
                    rule_confidence=flaky_conf,
                ),
            )

        # Ensure FLAKY_TEST ranks at the top
        adjusted_hypotheses.sort(
            key=lambda h: (h.failure_type == FailureTaxonomy.FLAKY_TEST, h.rule_confidence),
            reverse=True,
        )
        return adjusted_hypotheses

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