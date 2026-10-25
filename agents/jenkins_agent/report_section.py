"""Deterministic generator for Jenkins Findings report sections.

Produces both human-readable Markdown and machine-readable structured JSON
summarizing build identity, failures, classification, confidence breakdown,
evidence excerpts, unknown/missing aspects, and recommended next steps.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import re
from typing import Any

from common.models import Evidence, EvidenceItem, FailureTaxonomy
from common.redaction import redact

logger = logging.getLogger(__name__)

MAX_EXCERPT_LINES = 10
MIN_EXCERPTS = 3
MAX_EXCERPTS = 5


@dataclass
class FailureDetail:
    kind: str  # "test" | "compile_error" | "stage" | "other"
    name: str
    location: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "location": self.location,
            "message": self.message,
        }


@dataclass
class EvidenceExcerpt:
    evidence_id: str
    kind: str
    content: str
    line_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "content": self.content,
            "line_count": self.line_count,
        }


@dataclass
class JenkinsFindingsSection:
    """Container holding both rendered Markdown and structured JSON report sections."""

    markdown: str
    structured: dict[str, Any]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return self.structured

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.structured, indent=indent)

    def __str__(self) -> str:
        return self.markdown


def _truncate_lines(text: str, max_lines: int = MAX_EXCERPT_LINES) -> str:
    """Truncate text to at most max_lines lines, appending a truncation notice if needed."""
    lines = text.strip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    kept = lines[: max_lines - 1]
    kept.append(f"... [truncated, {len(lines) - max_lines + 1} more lines]")
    return "\n".join(kept)


def _extract_build_identity(evidence: Evidence) -> dict[str, Any]:
    """Extract job name, build number, commit, branch from evidence items."""
    identity: dict[str, Any] = {
        "incident_id": str(evidence.incident_id),
        "job_name": None,
        "build_number": None,
        "commit": None,
        "branch": None,
    }

    # Try build_summary item first
    for item in evidence.evidence_items:
        if item.id == "build_summary":
            try:
                payload = json.loads(item.content)
                if isinstance(payload, dict):
                    identity["job_name"] = payload.get("job_name")
                    identity["build_number"] = payload.get("build_number")
                    identity["commit"] = payload.get("commit") or payload.get("git_commit")
                    identity["branch"] = payload.get("branch")
            except (json.JSONDecodeError, TypeError):
                pass
            break

    # If missing, try parsing source string (e.g. jenkins:orders/main#7)
    if not identity["job_name"] or identity["build_number"] is None:
        for item in evidence.evidence_items:
            match = re.search(r"jenkins:(.+?)#(\d+)", item.source)
            if match:
                if not identity["job_name"]:
                    identity["job_name"] = match.group(1)
                if identity["build_number"] is None:
                    identity["build_number"] = int(match.group(2))
                break

    return identity


def _extract_top_failures(evidence: Evidence) -> list[FailureDetail]:
    """Extract top failing tests or compiler errors with file:line locations."""
    failures: list[FailureDetail] = []

    # 1. Compiler errors from evidence_items
    for item in evidence.evidence_items:
        if item.id.startswith("compiler_errors["):
            location_str = None
            if item.location and item.location.file:
                location_str = (
                    f"{item.location.file}:{item.location.line}"
                    if item.location.line is not None
                    else item.location.file
                )
            failures.append(
                FailureDetail(
                    kind="compile_error",
                    name="Compile Error",
                    location=location_str,
                    message=item.content,
                )
            )

    # 2. Failing tests from evidence_items
    for item in evidence.evidence_items:
        if item.id.startswith("failing_tests["):
            try:
                payload = json.loads(item.content)
                name = payload.get("name", "Unknown test")
                file_path = payload.get("file")
                line_no = payload.get("line")
                message = payload.get("message", "")
            except (json.JSONDecodeError, TypeError):
                name = "Test failure"
                file_path = item.location.file if item.location else None
                line_no = item.location.line if item.location else None
                message = item.content

            location_str = None
            if file_path:
                location_str = f"{file_path}:{line_no}" if line_no is not None else str(file_path)
            elif item.location and item.location.file:
                location_str = (
                    f"{item.location.file}:{item.location.line}"
                    if item.location.line is not None
                    else item.location.file
                )

            failures.append(
                FailureDetail(
                    kind="test",
                    name=name,
                    location=location_str,
                    message=message,
                )
            )

    # 3. Fallback: if evidence.failing_tests has names not captured above
    if not failures and evidence.failing_tests:
        for test_name in evidence.failing_tests:
            failures.append(
                FailureDetail(
                    kind="test",
                    name=test_name,
                    location=None,
                    message="Test failed in build execution",
                )
            )

    return failures[:5]


def _extract_confidence_breakdown(evidence: Evidence) -> dict[str, Any]:
    """Extract or reconstruct the auditable confidence breakdown."""
    for item in evidence.evidence_items:
        if item.id == "confidence_breakdown":
            try:
                payload = json.loads(item.content)
                if isinstance(payload, dict):
                    return payload
            except (json.JSONDecodeError, TypeError):
                pass

    # Fallback structure if confidence_breakdown item was not attached
    components = {
        "rule_match": round(evidence.confidence, 4),
        "llm_agreement": round(evidence.confidence, 4),
        "signal_strength": 0.8 if evidence.failed_stage else 0.5,
        "history_support": 0.7,
    }
    penalties = ["truncated_log: -0.20"] if evidence.log_truncated else []
    hyp_confidences = {
        h.failure_type.value: round(h.confidence, 4)
        for h in evidence.root_cause_hypotheses
    }
    if not hyp_confidences:
        hyp_confidences[evidence.failure_type.value] = round(evidence.confidence, 4)

    return {
        "total": round(evidence.confidence, 4),
        "components": components,
        "penalties": penalties,
        "hypothesis_confidences": hyp_confidences,
    }


def _select_key_excerpts(evidence: Evidence) -> list[EvidenceExcerpt]:
    """Select 3-5 prioritized evidence excerpts, redacted and capped at 10 lines."""
    selected_items: list[EvidenceItem] = []

    # Priority 1: Compiler errors & failing tests
    error_items = [
        item
        for item in evidence.evidence_items
        if item.id.startswith("compiler_errors[") or item.id.startswith("failing_tests[")
    ]
    selected_items.extend(error_items[:3])

    # Priority 2: Error blocks and error signature
    diagnostic_items = [
        item
        for item in evidence.evidence_items
        if item.id.startswith("error_blocks[") or item.id == "error_signature"
    ]
    for item in diagnostic_items:
        if len(selected_items) >= MAX_EXCERPTS:
            break
        if item not in selected_items:
            selected_items.append(item)

    # Priority 3: Stage failures, timeouts, auth signals
    other_candidates = [
        item
        for item in evidence.evidence_items
        if item.id not in {"confidence_breakdown", "build_summary", "build_history"}
        and item not in selected_items
    ]
    for item in other_candidates:
        if len(selected_items) >= MAX_EXCERPTS:
            break
        selected_items.append(item)

    # If still fewer than MIN_EXCERPTS, include build summary or remaining items
    if len(selected_items) < MIN_EXCERPTS:
        for item in evidence.evidence_items:
            if item.id != "confidence_breakdown" and item not in selected_items:
                selected_items.append(item)
            if len(selected_items) >= MIN_EXCERPTS:
                break

    excerpts: list[EvidenceExcerpt] = []
    for item in selected_items[:MAX_EXCERPTS]:
        raw_content = item.content
        # Pretty format JSON excerpts if applicable
        try:
            parsed_json = json.loads(raw_content)
            if isinstance(parsed_json, dict):
                # Extract message/error if structured
                if "message" in parsed_json:
                    raw_content = f"{parsed_json.get('name', '')}\n{parsed_json['message']}".strip()
                else:
                    raw_content = json.dumps(parsed_json, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass

        redacted_content = redact(raw_content)
        truncated_content = _truncate_lines(redacted_content, MAX_EXCERPT_LINES)
        line_count = len(truncated_content.splitlines())

        excerpts.append(
            EvidenceExcerpt(
                evidence_id=item.id,
                kind=item.kind,
                content=truncated_content,
                line_count=line_count,
            )
        )

    return excerpts


def _deduce_unknowns(evidence: Evidence) -> list[str]:
    """Deterministically list what information is unknown or missing."""
    unknowns: list[str] = []

    if evidence.log_truncated:
        unknowns.append("Console output was truncated; complete error stacktrace may be missing.")

    if not evidence.failed_stage:
        unknowns.append("Failed pipeline stage could not be identified from stages list.")

    has_test_or_compile = bool(
        evidence.failing_tests
        or any(
            i.id.startswith("compiler_errors") or i.id.startswith("failing_tests")
            for i in evidence.evidence_items
        )
    )
    if not has_test_or_compile:
        unknowns.append("No explicit failing test assertions or compiler errors isolated from logs.")

    if not evidence.error_signature:
        unknowns.append("Normalized error signature is empty.")

    has_history = any(i.id == "build_history" for i in evidence.evidence_items)
    if not has_history:
        unknowns.append("Build history unavailable to determine test flakiness or regression window.")

    if evidence.failure_type == FailureTaxonomy.UNKNOWN:
        unknowns.append("Root cause could not be mapped to a known failure taxonomy category.")

    if not unknowns:
        unknowns.append("None; diagnostic evidence is complete.")

    return unknowns


def _generate_fallback_summary(evidence: Evidence) -> str:
    """Generate a deterministic 2-sentence natural-language summary fallback."""
    stage_str = f"stage '{evidence.failed_stage}'" if evidence.failed_stage else "an unspecified stage"
    f_type = evidence.failure_type.value

    # Sentence 1: failure & diagnosed cause
    s1 = f"Jenkins build failed during {stage_str} due to {f_type} with {evidence.confidence:.0%} confidence."

    # Sentence 2: recommended next step
    if evidence.recommended_next_steps:
        next_step = evidence.recommended_next_steps[0].rstrip(".")
        s2 = f"The primary recommended action is to {next_step[0].lower() + next_step[1:]}."
    else:
        s2 = "Further inspection of CI logs around the point of failure is recommended."

    return f"{s1} {s2}"


def _call_llm_summary(evidence: Evidence, llm_client: Any) -> str | None:
    """Attempt to generate a 2-sentence summary using the provided LLM client."""
    if llm_client is None:
        return None

    stage_desc = evidence.failed_stage or "unknown"
    tests_desc = ", ".join(evidence.failing_tests[:3]) or "none isolated"
    error_desc = evidence.error_signature or "unspecified"
    prompt = (
        "You are a CI failure analyst. Write an exact 2-sentence natural-language summary "
        "of the following build failure.\n"
        "Sentence 1 must summarize what failed and the diagnosed cause.\n"
        "Sentence 2 must state the primary recommended next step.\n\n"
        f"Failure Type: {evidence.failure_type.value}\n"
        f"Confidence: {evidence.confidence:.2f}\n"
        f"Failed Stage: {stage_desc}\n"
        f"Failing Tests: {tests_desc}\n"
        f"Error Signature: {error_desc}\n"
        f"Next Steps: {evidence.recommended_next_steps}\n"
    )

    try:
        if hasattr(llm_client, "invoke"):
            from common.llm_client import HumanMessage

            resp = llm_client.invoke([HumanMessage(content=prompt)])
            text = getattr(resp, "content", str(resp))
        elif callable(llm_client):
            text = str(llm_client(prompt))
        else:
            return None

        clean_text = text.strip()
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", clean_text) if s.strip()]
        if len(sentences) >= 2:
            return f"{sentences[0]} {sentences[1]}"
        elif clean_text:
            return clean_text
    except Exception as exc:
        logger.warning("LLM summary generation failed, falling back to deterministic string (%s)", exc)

    return None


def generate_report_section(
    evidence: Evidence,
    llm_client: Any = None,
) -> JenkinsFindingsSection:
    """Generate deterministic Markdown and structured JSON 'Jenkins Findings' section.

    Args:
        evidence: Validated cross-agent Evidence object.
        llm_client: Optional LLM client used exclusively to generate the 2-sentence summary.

    Returns:
        JenkinsFindingsSection containing markdown string, structured dictionary, and summary.
    """
    # 1. 2-sentence Natural-language summary
    summary = _call_llm_summary(evidence, llm_client) or _generate_fallback_summary(evidence)

    # 2. Build identity
    build_identity = _extract_build_identity(evidence)

    # 3. Failed stage
    failed_stage = evidence.failed_stage or "Unknown"

    # 4. Top failures
    top_failures = _extract_top_failures(evidence)

    # 5. Classification & confidence breakdown
    breakdown = _extract_confidence_breakdown(evidence)
    classification_info = {
        "failure_type": evidence.failure_type.value,
        "confidence": round(evidence.confidence, 4),
        "components": breakdown.get("components", {}),
        "penalties": breakdown.get("penalties", []),
        "hypothesis_confidences": breakdown.get("hypothesis_confidences", {}),
    }

    # 6. Key evidence excerpts (3-5 items, max 10 lines each, redacted)
    key_excerpts = _select_key_excerpts(evidence)

    # 7. What's unknown / missing
    unknowns = _deduce_unknowns(evidence)

    # 8. Recommended next steps
    next_steps = evidence.recommended_next_steps or [
        "Inspect CI logs around point of failure."
    ]

    # --- Build Structured JSON Dict ---
    structured: dict[str, Any] = {
        "title": "Jenkins Findings",
        "summary": summary,
        "build_identity": build_identity,
        "failed_stage": failed_stage,
        "top_failures": [f.to_dict() for f in top_failures],
        "classification": classification_info,
        "key_evidence_excerpts": [e.to_dict() for e in key_excerpts],
        "unknown_or_missing": unknowns,
        "recommended_next_steps": next_steps,
    }

    # --- Render Markdown ---
    md_lines: list[str] = [
        "## Jenkins Findings",
        "",
        f"> {summary}",
        "",
        "### Build Identity",
        f"- **Incident ID:** `{build_identity['incident_id']}`",
    ]
    if build_identity["job_name"]:
        md_lines.append(f"- **Job Name:** `{build_identity['job_name']}`")
    if build_identity["build_number"] is not None:
        md_lines.append(f"- **Build Number:** `#{build_identity['build_number']}`")
    if build_identity["branch"]:
        md_lines.append(f"- **Branch:** `{build_identity['branch']}`")
    if build_identity["commit"]:
        md_lines.append(f"- **Git Commit:** `{build_identity['commit']}`")

    md_lines.extend([
        "",
        "### Failed Stage",
        f"- **Failed Stage:** `{failed_stage}`",
        "",
        "### Top Failures",
    ])

    if top_failures:
        for f in top_failures:
            loc_part = f" (`{f.location}`)" if f.location else ""
            msg_single_line = f.message.splitlines()[0] if f.message else ""
            md_lines.append(f"- **[{f.kind.upper()}]** `{f.name}`{loc_part}: {msg_single_line}")
    else:
        md_lines.append("- No explicit test failures or compiler errors isolated from logs.")

    md_lines.extend([
        "",
        "### Classification & Confidence",
        f"- **Failure Taxonomy:** `{evidence.failure_type.value}`",
        f"- **Overall Confidence:** `{evidence.confidence:.2f}` ({evidence.confidence:.1%})",
    ])

    components = classification_info.get("components", {})
    if components:
        md_lines.append("- **Score Components:**")
        for comp_name, comp_val in components.items():
            md_lines.append(f"  - `{comp_name}`: {comp_val:.4f}")

    penalties = classification_info.get("penalties", [])
    if penalties:
        md_lines.append("- **Penalties Applied:**")
        for penalty in penalties:
            md_lines.append(f"  - {penalty}")
    else:
        md_lines.append("- **Penalties Applied:** None")

    hyp_confs = classification_info.get("hypothesis_confidences", {})
    if hyp_confs:
        md_lines.append("- **Per-Hypothesis Confidences (Independent Beliefs):**")
        for hyp_type, hyp_conf in hyp_confs.items():
            md_lines.append(f"  - `{hyp_type}`: {hyp_conf:.4f}")

    md_lines.extend([
        "",
        "### Key Evidence Excerpts",
    ])

    for exc in key_excerpts:
        md_lines.extend([
            f"#### Evidence `[{exc.evidence_id}]` ({exc.kind}, {exc.line_count} lines)",
            "```",
            exc.content,
            "```",
        ])

    md_lines.extend([
        "",
        "### Unknown / Missing Information",
    ])
    for unk in unknowns:
        md_lines.append(f"- {unk}")

    md_lines.extend([
        "",
        "### Recommended Next Steps",
    ])
    for step in next_steps:
        md_lines.append(f"1. {step}")

    markdown_text = "\n".join(md_lines)

    return JenkinsFindingsSection(
        markdown=markdown_text,
        structured=structured,
        summary=summary,
    )


# Alias for flexible invocation
generate_jenkins_findings = generate_report_section
