"""Which layer decides what: ``actionable`` and OPA act; ``ready_for_policy_review`` only advises.

**Owner:** P2 (Week 5). The Coordinator produces two verdicts about whether a report may lead to a
fix: P3's ``IncidentReport.actionable`` (what the remediation engine and the policy gate read) and
the fused report's ``ready_for_policy_review`` (a precondition flag). They must never disagree in the
permissive direction, or the advisory flag could be read as a way around the real gate.

Two mechanisms keep that true and both are tested here: ``fuse`` itself refuses to call a score below
the shared reportable floor ready, and ``apply_gate`` (used by ``synthesize_fused``) forces the flag off
whenever the authoritative report is not actionable.
"""

from itertools import combinations_with_replacement

import pytest

from common.models import FailureTaxonomy
from coordinator.fusion import FusionConfig, apply_gate, fuse
from coordinator.report import BlockingCode
from coordinator.synthesis import MIN_REPORTABLE_CONFIDENCE, synthesize
from tests.coordinator.helpers import NOW, T, make, view
from tests.coordinator.scenario_runner import (
    config_for,
    load_coordinator_scenarios,
    scenario_id,
)

AGENTS = ("jenkins_agent", "metrics_agent", "code_agent")


def documents_for(scenario):
    return [
        make(
            agent,
            FailureTaxonomy(spec["failure_type"]),
            float(spec["confidence"]),
            status=spec.get("status", "completed"),
            coverage=spec.get("coverage"),
        )
        for agent, spec in (scenario.get("agents") or {}).items()
    ]


def reconciled(documents, config=None):
    fused = fuse(view(*documents), config or FusionConfig(), now=NOW)
    gate = synthesize(documents, now=NOW)
    return apply_gate(fused, actionable=gate.actionable), gate


def test_fuse_alone_never_calls_a_score_below_the_floor_ready():
    documents = [make(a, T.TIMEOUT, 0.2) for a in AGENTS]  # three weak agreeing votes: fused score ~0.34
    fused = fuse(view(*documents), FusionConfig(), now=NOW)
    assert fused.confidence < MIN_REPORTABLE_CONFIDENCE
    assert not fused.ready_for_policy_review
    assert BlockingCode.LOW_CONFIDENCE in {b.code for b in fused.blocking_reasons}


def test_apply_gate_overrides_a_ready_report_when_the_authoritative_report_refuses():
    # Two weak agreeing votes vs one stronger: fusion likes timeout, the agreement gate does not.
    documents = [make("jenkins_agent", T.TIMEOUT, 0.5), make("metrics_agent", T.TIMEOUT, 0.5), make("code_agent", T.CONFIG_ERROR, 0.65)]
    fused = fuse(view(*documents), FusionConfig(), now=NOW)
    gate = synthesize(documents, now=NOW)
    assert fused.ready_for_policy_review and not gate.actionable  # the disagreement exists...
    final = apply_gate(fused, actionable=gate.actionable)
    assert not final.ready_for_policy_review  # ...and is closed in the safe direction
    assert BlockingCode.GATE_REFUSED in {b.code for b in final.blocking_reasons}


def test_apply_gate_changes_nothing_when_the_gate_agrees():
    documents = [make("jenkins_agent", T.TIMEOUT, 0.9), make("metrics_agent", T.TIMEOUT, 0.8), make("code_agent", T.TIMEOUT, 0.7)]
    fused = fuse(view(*documents), FusionConfig(), now=NOW)
    assert apply_gate(fused, actionable=True) is fused


@pytest.mark.parametrize("scenario", load_coordinator_scenarios(), ids=scenario_id)
def test_no_scenario_is_ready_for_review_while_the_gate_refuses(scenario) -> None:
    final, gate = reconciled(documents_for(scenario), config_for(scenario))
    if final.ready_for_policy_review:
        assert gate.actionable, f"{scenario['id']}: advisory flag is more permissive than actionable"


def test_exhaustively_over_small_vote_combinations_ready_implies_actionable() -> None:
    kinds = [T.TIMEOUT, T.DEPENDENCY_REGRESSION, T.UNKNOWN]
    confidences = (0.2, 0.55, 0.9)
    checked = 0
    for chosen in combinations_with_replacement([(k, c) for k in kinds for c in confidences], 3):
        for dropped in (None, "code_agent"):
            documents = [make(a, kind, conf) for a, (kind, conf) in zip(AGENTS, chosen, strict=True) if a != dropped]
            final, gate = reconciled(documents)
            checked += 1
            if final.ready_for_policy_review:
                assert gate.actionable, [(d.agent, d.failure_type.value, d.confidence) for d in documents]
    assert checked > 100
