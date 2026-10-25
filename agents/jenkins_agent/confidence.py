"""Deterministic confidence scoring for Jenkins build failure investigation.

Confidence reflects evidence quality and diagnostic certainty, combining rule-based
pattern matching, LLM hypothesis alignment, parsed log signal strength, and build
history corroboration.

Formula:
--------
1. Components (each normalized to 0.0 - 1.0):
   * rule_match       (weight w_rule): Strength and quality of rule-based pattern
                        matches. Evaluated from the primary rule hypothesis confidence,
                        calibrated per failure pattern.
   * llm_agreement    (weight w_llm): Concordance between rule classification and
                        LLM diagnoses, modulated by the LLM's reported confidence.
                        When LLM is absent (offline mode), mirrors rule match.
   * signal_strength  (weight w_signal): Richness and specificity of structural signals
                        in ParsedBuild (identified failed stage, failing tests,
                        compiler errors, error signatures, error blocks, exit codes).
   * history_support  (weight w_history): Corroboration from previous build runs
                        (e.g. regression confirmation or flaky build patterns).
                        Defaults to neutral baseline (0.70) when history is empty.

   base_score = (
       w_rule * rule_match
       + w_llm * llm_agreement
       + w_signal * signal_strength
       + w_history * history_support
   )

2. Penalties & Caps:
   * empty_log: Log contains no usable build or failure signals. Capped at empty_log_cap (<= 0.20).
   * truncated_log: Console output was truncated. Deducts truncation_penalty (0.20).
   * partial_log: Build log was incomplete/partial. Deducts partial_penalty (0.10).
   * rule_llm_disagreement: Rule-based classification and LLM diagnosis disagree on failure type.
     Deducts disagreement_penalty (0.20).
   * unknown_classification: Failure cannot be matched to a known taxonomy category.
     Deducts unknown_penalty (0.25) and is capped at unknown_cap (<= 0.30).

3. Final Confidence:
   total = clamp(base_score - sum(penalties), min_confidence, max_confidence)
   Subject to empty_log_cap and unknown_cap.

4. Independent Beliefs:
   Per-hypothesis confidences represent independent beliefs and are NOT forced to sum
   to 1. The Coordinator synthesizes cross-agent evidence and performs Dempster-Shafer
   or multi-agent fusion.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any

from agents.jenkins_agent.classifier import Hypothesis
from agents.jenkins_agent.parsing import ParsedBuild
from common.models import EvidenceHypothesis, EvidenceItem, FailureTaxonomy


@dataclass(frozen=True)
class ConfidenceWeights:
    """Configurable weights, penalties, and caps for confidence scoring."""

    # Component weights (sum to 1.0)
    w_rule: float = 0.35
    w_llm: float = 0.25
    w_signal: float = 0.25
    w_history: float = 0.15

    # Penalties deducted from base confidence
    truncation_penalty: float = 0.20
    partial_penalty: float = 0.10
    disagreement_penalty: float = 0.20
    unknown_penalty: float = 0.25

    # Caps & bounds
    empty_log_cap: float = 0.20
    unknown_cap: float = 0.30
    min_confidence: float = 0.0
    max_confidence: float = 0.98


class HypothesisConfidenceDict(dict):
    """Dictionary supporting lookups by both FailureTaxonomy enum and string value."""

    def __getitem__(self, key: Any) -> float:
        if hasattr(key, "value"):
            key = key.value
        return super().__getitem__(str(key))

    def get(self, key: Any, default: Any = None) -> Any:
        if hasattr(key, "value"):
            key = key.value
        return super().get(str(key), default)

    def __contains__(self, key: Any) -> bool:
        if hasattr(key, "value"):
            key = key.value
        return super().__contains__(str(key))


@dataclass
class ConfidenceBreakdown:
    """Detailed audit trail and scoring breakdown of Jenkins build failure confidence."""

    total: float
    components: dict[str, float]
    penalties: list[str]
    hypothesis_confidences: dict[str, float] = field(default_factory=HypothesisConfidenceDict)

    def __iter__(self):
        """Allow tuple unpacking: total, components, penalties = breakdown."""
        return iter((self.total, self.components, self.penalties))

    @property
    def per_hypothesis(self) -> dict[str, float]:
        """Per-hypothesis confidence mapping (independent beliefs, not summed to 1)."""
        return self.hypothesis_confidences

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "components": dict(self.components),
            "penalties": list(self.penalties),
            "hypothesis_confidences": dict(self.hypothesis_confidences),
        }

    def as_evidence_item(
        self,
        source: str = "jenkins_agent:confidence",
        timestamp: datetime | None = None,
    ) -> EvidenceItem:
        return EvidenceItem(
            id="confidence_breakdown",
            kind="test_result",
            source=source,
            timestamp=timestamp,
            content=json.dumps(self.to_dict()),
        )


def _extract_log_meta(log_meta: Any) -> tuple[bool, bool, bool]:
    """Return (truncated, partial, is_empty) flags from log metadata."""
    if log_meta is None:
        return False, False, False
    if isinstance(log_meta, dict):
        truncated = bool(
            log_meta.get("truncated", False)
            or log_meta.get("log_truncated", False)
            or log_meta.get("console_truncated", False)
        )
        partial = bool(
            log_meta.get("partial", False)
            or log_meta.get("console_partial", False)
        )
        is_empty = bool(log_meta.get("is_empty", False) or log_meta.get("empty", False))
        if "text" in log_meta and not str(log_meta["text"]).strip():
            is_empty = True
        return truncated, partial, is_empty

    truncated = bool(getattr(log_meta, "truncated", False))
    partial = bool(getattr(log_meta, "partial", False))
    text = getattr(log_meta, "text", None)
    is_empty = bool(
        getattr(log_meta, "is_empty", False)
        or (text is not None and not str(text).strip())
    )
    return truncated, partial, is_empty


def _is_empty_parsed(parsed: ParsedBuild | None) -> bool:
    """Return True if ParsedBuild has no meaningful diagnostic signals."""
    if parsed is None:
        return True
    return (
        not parsed.stages
        and not parsed.failed_stage
        and not parsed.error_blocks
        and not parsed.failing_tests
        and not parsed.compiler_errors
        and not parsed.exit_codes
        and not parsed.timeouts
        and not parsed.auth_signals
        and not parsed.error_signature
    )


def _get_failure_type(h: Any) -> FailureTaxonomy:
    raw = getattr(h, "failure_type", None)
    if raw is None and isinstance(h, dict):
        raw = h.get("failure_type")
    if isinstance(raw, FailureTaxonomy):
        return raw
    if isinstance(raw, str):
        try:
            return FailureTaxonomy(raw)
        except ValueError:
            return FailureTaxonomy.UNKNOWN
    return FailureTaxonomy.UNKNOWN


def _get_hyp_confidence(h: Any) -> float:
    if hasattr(h, "confidence"):
        return float(h.confidence)
    if hasattr(h, "rule_confidence"):
        return float(h.rule_confidence)
    if isinstance(h, dict):
        return float(h.get("confidence", h.get("rule_confidence", 0.5)))
    return 0.5


def compute_confidence(
    parsed: ParsedBuild | None,
    rule_hypotheses: Sequence[Hypothesis] | None,
    llm_hypotheses: Sequence[EvidenceHypothesis | Hypothesis | dict[str, Any]] | None,
    log_meta: Any,
    history: Sequence[dict[str, Any]] | None,
    weights: ConfidenceWeights | None = None,
) -> ConfidenceBreakdown:
    """Compute overall and per-hypothesis confidence for Jenkins build failure analysis.

    Args:
        parsed: Structural failure signals parsed from Jenkins console log.
        rule_hypotheses: Deterministic hypotheses produced by rule-based classifier.
        llm_hypotheses: Hypotheses proposed by LLM reasoning, or None if offline.
        log_meta: Log metadata dictionary or ConsoleText object.
        history: Prior build history records.
        weights: Configurable weights and penalty values (defaults to ConfidenceWeights()).

    Returns:
        ConfidenceBreakdown with total overall confidence, components breakdown dict,
        penalties list, and per-hypothesis confidences.
    """
    if weights is None:
        weights = ConfidenceWeights()

    truncated, partial, is_empty_meta = _extract_log_meta(log_meta)
    empty = is_empty_meta or _is_empty_parsed(parsed)

    # 1. Rule component
    primary_rule = rule_hypotheses[0] if rule_hypotheses else None
    rule_type = primary_rule.failure_type if primary_rule else FailureTaxonomy.UNKNOWN
    if not rule_hypotheses or empty:
        rule_score = 0.0
    elif rule_type == FailureTaxonomy.UNKNOWN:
        rule_score = min(0.20, primary_rule.rule_confidence)
    else:
        rule_score = min(1.0, max(0.0, primary_rule.rule_confidence))

    # 2. LLM agreement component & disagreement detection
    primary_llm = llm_hypotheses[0] if llm_hypotheses else None
    disagreement = False
    llm_type = FailureTaxonomy.UNKNOWN

    if primary_llm is not None and not empty:
        llm_type = _get_failure_type(primary_llm)
        llm_conf = _get_hyp_confidence(primary_llm)

        if rule_type != FailureTaxonomy.UNKNOWN and llm_type != FailureTaxonomy.UNKNOWN:
            if rule_type == llm_type:
                llm_score = min(1.0, max(0.0, 0.30 + 0.70 * llm_conf))
            else:
                llm_score = 0.0
                disagreement = True
        elif llm_type != FailureTaxonomy.UNKNOWN:
            llm_score = min(1.0, max(0.0, llm_conf * 0.70))
        else:
            llm_score = 0.20
    elif empty:
        llm_score = 0.0
    else:
        # LLM not invoked / offline fallback: neutral, reflects rule evidence
        llm_score = rule_score

    # 3. Signal strength component
    if empty or parsed is None:
        signal_score = 0.0
    else:
        signals = 0.0
        if parsed.failed_stage:
            signals += 0.25
        if parsed.failing_tests or parsed.compiler_errors:
            signals += 0.35
        if parsed.error_blocks or parsed.error_signature:
            signals += 0.25
        if parsed.exit_codes or parsed.timeouts or parsed.auth_signals:
            signals += 0.15
        signal_score = min(1.0, signals)

    # 4. History support component
    if not history:
        history_score = 0.70  # Neutral baseline
    else:
        history_score = 0.85  # Corroborating history available

    # Weighted base score
    base_score = (
        weights.w_rule * rule_score
        + weights.w_llm * llm_score
        + weights.w_signal * signal_score
        + weights.w_history * history_score
    )

    # 5. Penalties and Caps
    penalties: list[str] = []
    total = base_score

    if empty:
        penalties.append(f"empty_log: log contains no failure signals (cap={weights.empty_log_cap:.2f})")
        total = min(total, weights.empty_log_cap)
    else:
        if truncated:
            penalties.append(f"truncated_log: console output was truncated (-{weights.truncation_penalty:.2f})")
            total -= weights.truncation_penalty
        if partial:
            penalties.append(f"partial_log: build was incomplete / partial (-{weights.partial_penalty:.2f})")
            total -= weights.partial_penalty
        if disagreement:
            penalties.append(
                f"rule_llm_disagreement: rules predicted {rule_type.value} but LLM predicted {llm_type.value} (-{weights.disagreement_penalty:.2f})"
            )
            total -= weights.disagreement_penalty
        if rule_type == FailureTaxonomy.UNKNOWN and (primary_llm is None or llm_type == FailureTaxonomy.UNKNOWN):
            penalties.append(f"unknown_classification: failure cause is unknown (cap={weights.unknown_cap:.2f})")
            total = min(total - weights.unknown_penalty, weights.unknown_cap)

    total = max(weights.min_confidence, min(weights.max_confidence, total))
    total = round(total, 4)

    components = {
        "rule_match": round(rule_score, 4),
        "llm_agreement": round(llm_score, 4),
        "signal_strength": round(signal_score, 4),
        "history_support": round(history_score, 4),
    }

    # 6. Per-hypothesis independent confidences
    # Do NOT normalize them to sum to 1 - they represent independent beliefs.
    hypothesis_confidences = HypothesisConfidenceDict()
    seen_types: set[FailureTaxonomy] = set()

    all_hypotheses: list[Any] = []
    if rule_hypotheses:
        all_hypotheses.extend(rule_hypotheses)
    if llm_hypotheses:
        all_hypotheses.extend(llm_hypotheses)

    rule_type_map = {h.failure_type: h.rule_confidence for h in (rule_hypotheses or [])}
    llm_type_map = {
        _get_failure_type(h): _get_hyp_confidence(h) for h in (llm_hypotheses or [])
    }

    all_types = list(dict.fromkeys([_get_failure_type(h) for h in all_hypotheses]))
    if not all_types:
        all_types = [FailureTaxonomy.UNKNOWN]

    for f_type in all_types:
        r_conf = rule_type_map.get(f_type)
        l_conf = llm_type_map.get(f_type)

        if r_conf is not None and l_conf is not None:
            # Corroborated between rule and LLM
            c = min(weights.max_confidence, 0.5 * r_conf + 0.5 * l_conf + 0.05)
        elif r_conf is not None:
            c = r_conf
        elif l_conf is not None:
            c = l_conf
        else:
            c = 0.20

        if f_type == FailureTaxonomy.UNKNOWN:
            c = min(c, weights.unknown_cap)

        if truncated:
            c *= (1.0 - weights.truncation_penalty)
        if partial:
            c *= (1.0 - weights.partial_penalty)
        if empty:
            c = min(c, weights.empty_log_cap)

        c = max(weights.min_confidence, min(weights.max_confidence, round(c, 4)))
        hypothesis_confidences[f_type.value] = c

    return ConfidenceBreakdown(
        total=total,
        components=components,
        penalties=penalties,
        hypothesis_confidences=hypothesis_confidences,
    )
