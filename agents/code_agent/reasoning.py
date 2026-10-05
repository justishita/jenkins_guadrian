"""Root-cause reasoning over code evidence.

**Owner:** P3. Turns the signals from ``diff_analysis`` into ranked, cited hypotheses
about why a build failed. Week 3 delivers it as deterministic rules rather than an LLM
call, for three reasons: the rules are the ground truth the Week 4+ LLM reasoning is
evaluated against, they run in the scenario tests without a network or an API quota,
and a rule that fires wrongly can be read and corrected.

Two behaviours matter more than any individual rule:

* **Absence of code evidence is evidence.** When nothing changed, the agent says so
  with low confidence instead of blaming the most recent commit it can see (TC-12).
* **Competing explanations lower confidence.** A commit that touches a dependency pin
  *and* ten source files is not strong evidence for either, and the score says so,
  so the Coordinator weighs it accordingly (TC-13).

The agent's own confidence floor lives here too: a best hypothesis below
``MIN_REPORTABLE_CONFIDENCE`` is reported as insufficient evidence rather than as an
answer (TC-14).
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from agents.code_agent.diff_analysis import (
    ConfigurationChange,
    DependencyChange,
    DiffAnalysis,
    FileRole,
)
from agents.code_agent.evidence_builder import EvidenceIndex
from common.models import EvidenceHypothesis, FailureTaxonomy


#: Below this the agent reports insufficient evidence instead of a classification.
#: It matches the policy gate's own minimum, so the agent never proposes a fix from
#: evidence OPA would refuse to act on anyway.
MIN_REPORTABLE_CONFIDENCE = 0.5

#: ``unknown`` is a statement of ignorance, so it may never be asserted confidently.
#: The shared evidence contract is read the same way by the Coordinator.
MAX_UNKNOWN_CONFIDENCE = 0.4

#: More than this and the report stops being a ranked shortlist.
MAX_HYPOTHESES = 4

#: Keys whose value is a positive quantity: zero or negative is almost always a fault.
POSITIVE_QUANTITY_KEY_RE = re.compile(
    r"(?i)(timeout|seconds|_ms$|port|size|bytes|limit|max|count|retries|interval|workers|depth)"
)

URL_KEY_RE = re.compile(r"(?i)(_url|_uri|_endpoint|_host)$")

NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")
BOOLEAN_VALUES = frozenset({"true", "false", "yes", "no", "on", "off", "1", "0"})


@dataclass(frozen=True, slots=True)
class IncidentContext:
    """What the Code agent knows about the failure, independent of the other agents.

    Deliberately small: the agent sees the incident event and GitHub, and nothing
    the Jenkins or metrics agents produced. Cross-domain correlation is the
    Coordinator's job.
    """

    failed_stage: str | None = None
    branch: str = ""
    git_commit: str = ""
    remediation_attempt: int = 0

    def stage_mentions(self, *keywords: str) -> bool:
        stage = (self.failed_stage or "").lower()
        return any(keyword in stage for keyword in keywords)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, round(value, 2)))


def _value_kind(value: str | None) -> str:
    """Classify a configuration value so a type change can be spotted."""
    if value is None:
        return "absent"
    stripped = value.strip()
    if not stripped or stripped.lower() in {"null", "none", "~"}:
        return "empty"
    if stripped.lower() in BOOLEAN_VALUES and not NUMERIC_RE.match(stripped):
        return "boolean"
    if NUMERIC_RE.match(stripped):
        return "number"
    return "string"


def _url_scheme(value: str | None) -> str | None:
    if not value or "://" not in value:
        return None
    return value.split("://", 1)[0].lower()


def suspicious_configuration_reasons(change: ConfigurationChange) -> tuple[str, ...]:
    """Say what is wrong with a changed configuration value, if anything.

    These are the checkable signals - a value losing its type, a positive quantity
    going non-positive, a URL changing scheme - that separate "a config value moved"
    from "a config value was broken". Returning the reasons rather than a score keeps
    them quotable in the hypothesis a reviewer reads.
    """
    reasons: list[str] = []
    previous_kind = _value_kind(change.previous_value)
    new_kind = _value_kind(change.new_value)

    if change.new_value is None:
        reasons.append(f"{change.key} was removed")
        return tuple(reasons)

    if new_kind == "empty" and previous_kind not in {"empty", "absent"}:
        reasons.append(f"{change.key} was emptied")
    elif previous_kind not in {"absent", "empty"} and new_kind != previous_kind:
        reasons.append(f"{change.key} changed type from {previous_kind} to {new_kind}")

    if new_kind == "number" and POSITIVE_QUANTITY_KEY_RE.search(change.key):
        try:
            if float(change.new_value) <= 0:
                reasons.append(f"{change.key} is not a positive value ({change.new_value})")
        except ValueError:  # pragma: no cover - guarded by NUMERIC_RE
            pass

    if URL_KEY_RE.search(change.key):
        previous_scheme, new_scheme = _url_scheme(change.previous_value), _url_scheme(change.new_value)
        if previous_scheme and new_scheme and previous_scheme != new_scheme:
            reasons.append(f"{change.key} scheme changed from {previous_scheme} to {new_scheme}")
        elif previous_scheme and not new_scheme:
            reasons.append(f"{change.key} is no longer a URL")

    return tuple(reasons)


def _describe_dependencies(changes: tuple[DependencyChange, ...]) -> str:
    described = [change.describe() for change in changes[:3]]
    if len(changes) > 3:
        described.append(f"and {len(changes) - 3} more")
    return "; ".join(described)


def _dependency_hypothesis(
    analysis: DiffAnalysis, context: IncidentContext, index: EvidenceIndex
) -> EvidenceHypothesis | None:
    """TC-03: a dependency pin moved, and the build broke."""
    changes = analysis.dependency_changes
    if not changes:
        return None

    confidence = 0.6
    notes: list[str] = []

    # A version that moved is a far better suspect than one merely added: the build
    # worked with the old pin, and nothing else about that package changed.
    if any(change.is_upgrade_or_downgrade for change in changes):
        confidence += 0.1
        notes.append("a pinned version moved")
    if context.stage_mentions("build", "install", "test", "compile"):
        confidence += 0.1
        notes.append(f"the {context.failed_stage} stage is where dependency problems surface")

    source_paths = analysis.paths_with_role(FileRole.SOURCE)
    if not source_paths:
        confidence += 0.1
        notes.append("no source files changed alongside it")
    else:
        # A competing explanation is a real reduction in confidence, not a footnote.
        confidence -= 0.2
        notes.append(f"{len(source_paths)} source file(s) also changed, which could explain it instead")

    supporting = index.known(
        *(index.dependency_id(change) for change in changes),
        *index.file_ids_for(analysis, FileRole.DEPENDENCY_MANIFEST),
        index.head_commit_id,
    )
    contradicting = index.known(*index.file_ids_for(analysis, FileRole.SOURCE)) if source_paths else ()

    return EvidenceHypothesis(
        hypothesis=(
            f"A dependency change in this commit broke the build: {_describe_dependencies(changes)}. "
            + "; ".join(notes)
            + ". Pinning the package back to its previous version should restore the build."
        ),
        failure_type=FailureTaxonomy.DEPENDENCY_REGRESSION,
        confidence=_clamp(confidence),
        supporting_evidence=list(supporting),
        contradicting_evidence=list(contradicting),
    )


def _configuration_hypothesis(
    analysis: DiffAnalysis, context: IncidentContext, index: EvidenceIndex
) -> EvidenceHypothesis | None:
    """TC-08: a configuration value changed to something the application rejects."""
    changes = analysis.configuration_changes
    if not changes:
        return None

    suspicious: list[tuple[ConfigurationChange, tuple[str, ...]]] = [
        (change, reasons)
        for change in changes
        if (reasons := suspicious_configuration_reasons(change))
    ]

    confidence = 0.55
    notes: list[str] = []

    if suspicious:
        # The difference between "a value moved" and "a value was broken".
        confidence += 0.2
        notes.append("; ".join(reason for _, reasons in suspicious for reason in reasons))
    else:
        notes.append("the changed values are each individually plausible")

    source_paths = analysis.paths_with_role(FileRole.SOURCE)
    if not source_paths:
        confidence += 0.1
        notes.append("no source files changed alongside it")
    else:
        confidence -= 0.2
        notes.append(f"{len(source_paths)} source file(s) also changed")

    if context.stage_mentions("deploy", "start", "test", "build"):
        confidence += 0.05

    cited = [change for change, _ in suspicious] or list(changes)
    supporting = index.known(
        *(index.configuration_id(change) for change in cited),
        *index.file_ids_for(analysis, FileRole.CONFIGURATION),
        index.head_commit_id,
    )
    contradicting = index.known(*index.file_ids_for(analysis, FileRole.SOURCE)) if source_paths else ()

    headline = "; ".join(change.describe() for change in cited[:3])
    return EvidenceHypothesis(
        hypothesis=(
            f"A configuration change in this commit broke the build: {headline}. "
            + ". ".join(notes)
            + ". Restoring the previous value should restore the build."
        ),
        failure_type=FailureTaxonomy.CONFIG_ERROR,
        confidence=_clamp(confidence),
        supporting_evidence=list(supporting),
        contradicting_evidence=list(contradicting),
    )


def _source_change_hypothesis(
    analysis: DiffAnalysis, context: IncidentContext, index: EvidenceIndex
) -> EvidenceHypothesis | None:
    """A source or test change, classified by the stage that failed."""
    source_paths = analysis.paths_with_role(FileRole.SOURCE)
    test_paths = analysis.paths_with_role(FileRole.TEST)
    if not source_paths and not test_paths:
        return None

    if context.stage_mentions("build", "compile"):
        failure_type = FailureTaxonomy.BUILD_COMPILATION_FAILURE
        confidence = 0.6
        description = "failed at the build stage, so the change is likely not to compile or import"
    elif context.stage_mentions("test"):
        failure_type = FailureTaxonomy.CODE_TEST_FAILURE
        confidence = 0.6
        description = "failed at the test stage, so the change likely broke an assertion or behaviour"
    else:
        # Without a stage, a source change is a suspect but not a diagnosis. The
        # Jenkins agent's evidence is what will narrow this, via the Coordinator.
        failure_type = FailureTaxonomy.CODE_TEST_FAILURE
        confidence = 0.4
        description = "changed application code, though the failing stage is unknown"

    changed = len(source_paths) + len(test_paths)
    if changed > 5:
        # A large change set means the real cause is somewhere inside it, not that
        # we have found it.
        confidence -= 0.15
    if analysis.dependency_changes or analysis.configuration_changes:
        confidence -= 0.15

    supporting = index.known(
        *index.file_ids_for(analysis, FileRole.SOURCE),
        *index.file_ids_for(analysis, FileRole.TEST),
        index.head_commit_id,
    )
    return EvidenceHypothesis(
        hypothesis=(
            f"This commit {description}. It changed {changed} source/test file(s): "
            f"{', '.join((source_paths + test_paths)[:4])}."
        ),
        failure_type=failure_type,
        confidence=_clamp(confidence),
        supporting_evidence=list(supporting),
        contradicting_evidence=[],
    )


def _deployment_hypothesis(
    analysis: DiffAnalysis, context: IncidentContext, index: EvidenceIndex
) -> EvidenceHypothesis | None:
    """A deployment script or image definition changed and deployment failed."""
    deployment_paths = analysis.paths_with_role(FileRole.DEPLOYMENT)
    if not deployment_paths:
        return None

    confidence = 0.6 if context.stage_mentions("deploy", "release", "publish") else 0.4
    return EvidenceHypothesis(
        hypothesis=(
            "This commit changed deployment definitions "
            f"({', '.join(deployment_paths[:3])}), which matches a deployment-stage failure."
        ),
        failure_type=FailureTaxonomy.DEPLOYMENT_FAILURE,
        confidence=_clamp(confidence),
        supporting_evidence=list(
            index.known(*index.file_ids_for(analysis, FileRole.DEPLOYMENT), index.head_commit_id)
        ),
        contradicting_evidence=[],
    )


def _no_code_evidence_hypothesis(
    analysis: DiffAnalysis, context: IncidentContext, index: EvidenceIndex
) -> EvidenceHypothesis:
    """TC-12: say that the code is not the cause, rather than inventing one.

    This is the hypothesis the agent is most often right about, and the one an
    eager-to-help reasoner is most likely to skip.
    """
    if analysis.is_empty:
        statement = (
            f"Commit {context.git_commit[:7]} changed no files, so a recent code change does not "
            "explain this failure. The cause is more likely infrastructure, environment or a "
            "non-deterministic test."
        )
    else:
        statement = (
            "This commit changed only documentation, which cannot affect the build. A recent code "
            "change does not explain this failure."
        )

    return EvidenceHypothesis(
        hypothesis=statement,
        failure_type=FailureTaxonomy.UNKNOWN,
        confidence=0.3,
        supporting_evidence=list(index.known(index.head_commit_id)),
        contradicting_evidence=list(index.known(*index.file_ids_for(analysis, FileRole.DOCUMENTATION))),
    )


@dataclass(frozen=True, slots=True)
class ReasoningResult:
    """The agent's conclusion: ranked hypotheses and what it will report."""

    hypotheses: tuple[EvidenceHypothesis, ...]
    failure_type: FailureTaxonomy
    confidence: float
    status: str
    summary: str
    recommended_next_steps: tuple[str, ...]

    @property
    def best(self) -> EvidenceHypothesis | None:
        return self.hypotheses[0] if self.hypotheses else None


def reason(
    analysis: DiffAnalysis, context: IncidentContext, index: EvidenceIndex
) -> ReasoningResult:
    """Rank the plausible code-side causes of a failure and decide what to report."""
    if analysis.is_empty or analysis.touches_only_documentation:
        hypotheses = (_no_code_evidence_hypothesis(analysis, context, index),)
    else:
        candidates = [
            _dependency_hypothesis(analysis, context, index),
            _configuration_hypothesis(analysis, context, index),
            _source_change_hypothesis(analysis, context, index),
            _deployment_hypothesis(analysis, context, index),
        ]
        hypotheses = tuple(
            sorted(
                (item for item in candidates if item is not None),
                key=lambda item: item.confidence,
                reverse=True,
            )
        )[:MAX_HYPOTHESES]

    # `unknown` may never be asserted confidently, whatever the rules computed.
    hypotheses = tuple(
        hypothesis.model_copy(
            update={"confidence": min(hypothesis.confidence, MAX_UNKNOWN_CONFIDENCE)}
        )
        if hypothesis.failure_type is FailureTaxonomy.UNKNOWN
        else hypothesis
        for hypothesis in hypotheses
    )

    best = hypotheses[0] if hypotheses else None
    if best is None:
        return ReasoningResult(
            hypotheses=(),
            failure_type=FailureTaxonomy.UNKNOWN,
            confidence=0.1,
            status="insufficient_evidence",
            summary="No code-side hypothesis could be formed for this incident.",
            recommended_next_steps=("Correlate with the Jenkins and metrics evidence.",),
        )

    if best.confidence < MIN_REPORTABLE_CONFIDENCE:
        # TC-14: below the floor the agent reports what it saw, not what it suspects.
        return ReasoningResult(
            hypotheses=hypotheses,
            failure_type=FailureTaxonomy.UNKNOWN,
            confidence=best.confidence,
            status="insufficient_evidence",
            summary=(
                "The code evidence is not strong enough to name a root cause "
                f"(best hypothesis {best.confidence:.2f}, below {MIN_REPORTABLE_CONFIDENCE:.2f}). "
                f"{best.hypothesis}"
            ),
            recommended_next_steps=(
                "Correlate with the Jenkins and metrics evidence before proposing a fix.",
                "Compare against the last successful build on this branch.",
            ),
        )

    return ReasoningResult(
        hypotheses=hypotheses,
        failure_type=best.failure_type,
        confidence=best.confidence,
        status="completed",
        summary=best.hypothesis,
        recommended_next_steps=_next_steps_for(best.failure_type),
    )


def _next_steps_for(failure_type: FailureTaxonomy) -> tuple[str, ...]:
    """Concrete, checkable next steps - never "disable the test"."""
    steps: dict[FailureTaxonomy, tuple[str, ...]] = {
        FailureTaxonomy.DEPENDENCY_REGRESSION: (
            "Pin the changed dependency back to the version from the last successful build.",
            "Re-run the pipeline to confirm the pin restores it.",
        ),
        FailureTaxonomy.CONFIG_ERROR: (
            "Restore the previous value of the changed configuration key.",
            "Check the application's settings validation for the expected type and range.",
        ),
        FailureTaxonomy.CODE_TEST_FAILURE: (
            "Review the changed source against the failing assertion.",
            "Correlate with the Jenkins agent's test-report evidence.",
        ),
        FailureTaxonomy.BUILD_COMPILATION_FAILURE: (
            "Review the changed source for a syntax or import error.",
            "Correlate with the compiler output in the Jenkins evidence.",
        ),
        FailureTaxonomy.DEPLOYMENT_FAILURE: (
            "Review the changed deployment definition against the failing deploy step.",
        ),
    }
    return steps.get(failure_type, ("Correlate with the Jenkins and metrics evidence.",))


__all__ = [
    "MAX_HYPOTHESES",
    "MAX_UNKNOWN_CONFIDENCE",
    "MIN_REPORTABLE_CONFIDENCE",
    "IncidentContext",
    "ReasoningResult",
    "reason",
    "suspicious_configuration_reasons",
]
