"""Week 8: how accurate is the Coordinator, and does fusing the agents beat trusting one of them?

**Owner:** P2. The Coordinator's report is what the remediation engine acts on, so the questions are the ones that matter
for guarded remediation: how often is the verdict right, how often does it *act* (the report is ``actionable``), and when
it acts, how often is it wrong? Every method sees the same agent outputs:

* ``authoritative``  P3's ``coordinator.synthesis.synthesize`` (agreement bonus, conflict penalty): what the system uses
* ``noisy_or``       P2's advisory fusion (``coordinator.fusion``): not used by the live Coordinator, compared for Week 8
* ``highest_confidence``  baseline: believe the single most confident agent
* ``majority_vote``  baseline: the verdict most agents named
* ``only_<agent>``   each agent alone, to see whether fusion beats the best single agent

Two datasets:

**A. The labelled scenarios** (``scenarios/coord_*.yaml``): hand-written cases with a stated cause. Few (9 labelled), and
written by the people who wrote the Coordinator, so they can show a method misbehaving but cannot show it is good.

**B. A simulation** (seeded, so every run is identical): for each failure type, three agents answer correctly with a
probability set by their skill and their relevance to that type, abstain, or answer wrongly and overconfidently; errors can
be independent or shared (several agents misled the same way, like overlapping evidence). This is a *simulation of the
inputs*, not measured accuracy of the real agents: its value is to show how each fusion method behaves when agents err,
which the few real incidents cannot. The report states every assumption and runs the relevance model both ways
(the proposed weights, which P2's fusion also uses, and a flat one) so the simulation does not favour its own inputs.

    python scripts/evaluate_coordinator.py                 # 500 simulated incidents per condition
    python scripts/evaluate_coordinator.py --n 2000 --seed 7

Writes ``results/coordinator_eval.json`` and ``results/coordinator_eval.md``. Scores are reported, not asserted.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import sys
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.a2a_contract import AGENT_NAMES  # noqa: E402
from common.models import FailureTaxonomy  # noqa: E402
from coordinator.fusion import AGENT_RELEVANCE, FusionConfig, fuse  # noqa: E402
from coordinator.report import FusionMethod  # noqa: E402
from coordinator.synthesis import MIN_REPORTABLE_CONFIDENCE, synthesize  # noqa: E402
from tests.coordinator.helpers import NOW, make, view  # noqa: E402
from tests.coordinator.scenario_runner import load_coordinator_scenarios  # noqa: E402

T = FailureTaxonomy
CAUSES = [t for t in T if t is not T.UNKNOWN]
ABSTAINING = {"insufficient_evidence", "failed"}

# --- simulation assumptions (all printed in the report) -------------------------------------------------------
SKILLS = {"high": 0.95, "medium": 0.80, "low": 0.60}  # chance an agent with relevance 1.0 names the right cause
SHARED_ERROR = {"independent": 0.0, "shared": 0.6}  # chance a wrong answer is the same wrong cause as the others'
RELEVANCE = {"proposed": lambda agent, cause: AGENT_RELEVANCE[agent][cause], "flat": lambda agent, cause: 0.8}
ABSTAIN_SHARE = 0.6  # of the runs an agent does not get right, this share are honest "unknown"; the rest are wrong
CONF_RIGHT = (0.65, 0.95)  # confidence an agent reports when right
CONF_WRONG = (0.50, 0.85)  # ...and when wrong: deliberately overlapping, so confidence is only weakly informative
CONF_ABSTAIN = (0.10, 0.40)


@dataclass(frozen=True)
class Spec:
    failure_type: T
    confidence: float
    status: str = "completed"

    @property
    def abstained(self) -> bool:
        return self.status in ABSTAINING or self.failure_type is T.UNKNOWN


@dataclass(frozen=True)
class Decision:
    verdict: str
    confidence: float
    acts: bool  # the system would go on to propose a fix
    conflicted: bool = False


Inputs = dict[str, Spec]
Overrides = dict[str, Any] | None  # a scenario's own fusion settings (only P2's fusion has any)


# ------------------------------------------------------------------------------------------------------ methods


def _evidence(inputs: Inputs):
    return [make(agent, spec.failure_type, spec.confidence, status=spec.status) for agent, spec in inputs.items()]


def authoritative(inputs: Inputs, overrides: Overrides = None) -> Decision:
    outcomes = {agent: ("failed" if spec.status == "failed" else "completed") for agent, spec in inputs.items()}
    report = synthesize(_evidence(inputs), now=NOW, outcomes=outcomes)
    return Decision(report.failure_type.value, report.confidence, report.actionable, bool(report.conflicts) or report.status == "conflicting")


def noisy_or(inputs: Inputs, overrides: Overrides = None) -> Decision:
    config = replace(FusionConfig(), **(overrides or {}), method=FusionMethod.NOISY_OR)  # as tests/coordinator/scenario_runner does
    report = fuse(view(*_evidence(inputs)), config, now=NOW)
    return Decision(report.verdict.value, report.confidence, report.ready_for_policy_review, report.conflict_status.value != "none")


def _answering(inputs: Inputs) -> list[tuple[str, Spec]]:
    return [(agent, spec) for agent, spec in sorted(inputs.items()) if not spec.abstained]


def highest_confidence(inputs: Inputs, overrides: Overrides = None) -> Decision:
    answering = _answering(inputs)
    if not answering:
        return Decision("unknown", 0.0, False)
    _, best = max(answering, key=lambda item: item[1].confidence)
    return Decision(best.failure_type.value, best.confidence, best.confidence >= MIN_REPORTABLE_CONFIDENCE)


def majority_vote(inputs: Inputs, overrides: Overrides = None) -> Decision:
    votes: dict[str, list[float]] = defaultdict(list)
    for _, spec in _answering(inputs):
        votes[spec.failure_type.value].append(spec.confidence)
    if not votes:
        return Decision("unknown", 0.0, False)
    verdict, confidences = max(votes.items(), key=lambda item: (len(item[1]), sum(item[1])))
    mean = sum(confidences) / len(confidences)
    return Decision(verdict, mean, mean >= MIN_REPORTABLE_CONFIDENCE, conflicted=len(votes) > 1)


def only(agent: str) -> Callable[..., Decision]:
    def method(inputs: Inputs, overrides: Overrides = None) -> Decision:
        spec = inputs.get(agent)
        if spec is None or spec.abstained:
            return Decision("unknown", 0.0, False)
        return Decision(spec.failure_type.value, spec.confidence, spec.confidence >= MIN_REPORTABLE_CONFIDENCE)

    method.__name__ = f"only_{agent}"
    return method


METHODS: dict[str, Callable[..., Decision]] = {
    "authoritative": authoritative,
    "noisy_or": noisy_or,
    "highest_confidence": highest_confidence,
    "majority_vote": majority_vote,
    **{f"only_{agent.removesuffix('_agent')}": only(agent) for agent in AGENT_NAMES},
}


# ----------------------------------------------------------------------------------------------------- metrics


def ece_and_brier(pairs: list[tuple[float, bool]], bins: int = 10) -> tuple[float | None, float | None]:
    """Expected calibration error and Brier score of (confidence, was-right) pairs."""
    if not pairs:
        return None, None
    brier = sum((confidence - float(right)) ** 2 for confidence, right in pairs) / len(pairs)
    buckets: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    for confidence, right in pairs:
        buckets[min(bins - 1, int(confidence * bins))].append((confidence, right))
    ece = sum(
        len(bucket) / len(pairs) * abs(sum(c for c, _ in bucket) / len(bucket) - sum(r for _, r in bucket) / len(bucket))
        for bucket in buckets.values()
    )
    return round(ece, 4), round(brier, 4)


def summarize(cases: list[tuple[str | None, Decision]]) -> dict[str, Any]:
    """Score decisions against truth. ``truth`` None marks a genuinely ambiguous case: excluded from accuracy, but acting on
    it still counts (a system that confidently acts on an ambiguous incident is wrong whichever answer it gave)."""
    labelled = [(truth, d) for truth, d in cases if truth is not None]
    ambiguous = [d for truth, d in cases if truth is None]
    right = [d for truth, d in labelled if d.verdict == truth]
    acting = [(truth, d) for truth, d in labelled if d.acts]
    wrong_acting = [(truth, d) for truth, d in acting if d.verdict != truth]
    n = len(labelled)
    ece, brier = ece_and_brier([(d.confidence, d.verdict == truth) for truth, d in labelled])
    return {
        "n": n,
        "accuracy": round(len(right) / n, 4) if n else None,
        "acts": round(len(acting) / n, 4) if n else None,
        "precision_when_acting": round((len(acting) - len(wrong_acting)) / len(acting), 4) if acting else None,
        "wrong_and_acting": round(len(wrong_acting) / n, 4) if n else None,
        "acting_despite_conflict": round(sum(1 for _, d in acting if d.conflicted) / len(acting), 4) if acting else None,
        "ece": ece,
        "brier": brier,
        "ambiguous_n": len(ambiguous),
        "ambiguous_acted_on": sum(1 for d in ambiguous if d.acts),
    }


# ---------------------------------------------------------------------------------------------------- datasets


def scenario_inputs(scenario: dict[str, Any]) -> Inputs:
    return {
        agent: Spec(T(spec["failure_type"]), float(spec["confidence"]), spec.get("status", "completed"))
        for agent, spec in (scenario.get("agents") or {}).items()
    }


def labelled_cases() -> list[tuple[str, str | None, Inputs, dict[str, Any]]]:
    cases = []
    for scenario in load_coordinator_scenarios():
        truth = scenario.get("truth")
        cases.append((str(scenario["id"]), None if truth is None else str(truth), scenario_inputs(scenario), dict(scenario.get("config") or {})))
    return cases


def simulate_case(rng: random.Random, truth: T, skill: float, shared: float, relevance: Callable[[str, T], float]) -> Inputs:
    wrong_shared = rng.choice([c for c in CAUSES if c is not truth])
    inputs: Inputs = {}
    for agent in AGENT_NAMES:
        p_right = min(0.98, skill * relevance(agent, truth))
        roll = rng.random()
        if roll < p_right:
            inputs[agent] = Spec(truth, round(rng.uniform(*CONF_RIGHT), 2))
        elif roll < p_right + (1 - p_right) * ABSTAIN_SHARE:
            inputs[agent] = Spec(T.UNKNOWN, round(rng.uniform(*CONF_ABSTAIN), 2), "insufficient_evidence")
        else:
            wrong = wrong_shared if rng.random() < shared else rng.choice([c for c in CAUSES if c is not truth])
            inputs[agent] = Spec(wrong, round(rng.uniform(*CONF_WRONG), 2))
    return inputs


def conditions() -> list[tuple[str, float, float, str]]:
    return [(f"{skill}/{err}/{rel}", SKILLS[skill], SHARED_ERROR[err], rel) for skill in SKILLS for err in SHARED_ERROR for rel in RELEVANCE]


def run_simulation(n: int, seed: int) -> dict[str, dict[str, dict[str, Any]]]:
    results: dict[str, dict[str, dict[str, Any]]] = {}
    for name, skill, shared, rel in conditions():
        rng = random.Random(f"{seed}:{name}")
        incidents = [(truth.value, simulate_case(rng, truth, skill, shared, RELEVANCE[rel])) for truth in (rng.choice(CAUSES) for _ in range(n))]
        results[name] = {method: summarize([(truth, fn(inputs)) for truth, inputs in incidents]) for method, fn in METHODS.items()}
    return results


def run_labelled() -> dict[str, Any]:
    cases = labelled_cases()
    per_method = {method: summarize([(truth, fn(inputs, overrides)) for _, truth, inputs, overrides in cases]) for method, fn in METHODS.items()}
    misses: dict[str, list[str]] = {}
    for method, fn in METHODS.items():
        misses[method] = [
            f"{case_id}: said {fn(inputs, overrides).verdict}, truth {truth}"
            for case_id, truth, inputs, overrides in cases
            if truth is not None and fn(inputs, overrides).verdict != truth
        ]
    return {"cases": len(cases), "labelled": sum(1 for _, truth, _, _ in cases if truth is not None), "per_method": per_method, "misses": misses}


# --------------------------------------------------------------------------------------------------- reporting


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


def table(rows: dict[str, dict[str, Any]]) -> list[str]:
    ambiguous = any(s["ambiguous_n"] for s in rows.values())
    header = "| Method | Accuracy | Acts | Right when acting | **Wrong and acting** | Acts despite conflict |" + (" Acts on ambiguous |" if ambiguous else "") + " ECE | Brier |"
    out = [header, "|" + "---|" * (header.count("|") - 1)]
    for method, s in rows.items():
        cells = [
            f"`{method}`", pct(s["accuracy"]), pct(s["acts"]), pct(s["precision_when_acting"]), f"**{pct(s['wrong_and_acting'])}**",
            pct(s["acting_despite_conflict"]),
        ]
        if ambiguous:
            cells.append(f"{s['ambiguous_acted_on']}/{s['ambiguous_n']}")
        cells += [str(s["ece"]), str(s["brier"])]
        out.append("| " + " | ".join(cells) + " |")
    return out


def findings(simulation: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    """Facts computed from the numbers, not narrative: the strongest and weakest condition for the live Coordinator."""
    lines = []
    singles = [m for m in METHODS if m.startswith("only_")]
    beats = sum(
        1
        for per in simulation.values()
        if (per["authoritative"]["accuracy"] or 0) > max(per[m]["accuracy"] or 0 for m in singles)
    )
    lines.append(f"The authoritative Coordinator was more accurate than the best single agent in {beats} of {len(simulation)} conditions.")
    for baseline in ("highest_confidence", "majority_vote", "noisy_or"):
        diffs = [100 * ((per["authoritative"]["accuracy"] or 0) - (per[baseline]["accuracy"] or 0)) for per in simulation.values()]
        wins = sum(1 for d in diffs if d > 0)
        ties = sum(1 for d in diffs if d == 0)
        safer = sum(1 for per in simulation.values() if (per["authoritative"]["wrong_and_acting"] or 0) < (per[baseline]["wrong_and_acting"] or 0))
        lines.append(
            f"Versus `{baseline}`: accuracy more in {wins}, equal in {ties}, less in {len(simulation) - wins - ties} conditions "
            f"(average {sum(diffs) / len(diffs):+.1f} points); wrong-and-acting lower in {safer} of {len(simulation)}."
        )
    worst_name, worst = max(simulation.items(), key=lambda item: item[1]["authoritative"]["wrong_and_acting"] or 0)
    lines.append(
        f"Worst case for the authoritative Coordinator: `{worst_name}`, where it was wrong *and* went on to act in "
        f"{pct(worst['authoritative']['wrong_and_acting'])} of incidents."
    )
    shared = [per["authoritative"]["wrong_and_acting"] or 0 for name, per in simulation.items() if "/shared/" in name]
    independent = [per["authoritative"]["wrong_and_acting"] or 0 for name, per in simulation.items() if "/independent/" in name]
    if shared and independent:
        lines.append(
            f"Its wrong-and-acting rate averaged {pct(sum(independent) / len(independent))} when the agents' errors were independent "
            f"and {pct(sum(shared) / len(shared))} when they shared a misleading signal."
        )
    return lines


def render_markdown(labelled: dict[str, Any], simulation: dict[str, Any], n: int, seed: int, generated_at: str) -> str:
    out = [
        "# Coordinator evaluation",
        "",
        f"_Generated {generated_at} by `scripts/evaluate_coordinator.py` (simulation: n={n} per condition, seed {seed})._",
        "",
        "**What the columns mean.** *Accuracy*: the verdict equals the true cause. *Acts*: the report would be actionable, so a fix is proposed. "
        "*Right when acting*: of the incidents it acts on, how many were right. **Wrong and acting**: wrong *and* acted on, the dangerous case "
        "for guarded remediation. *Acts despite conflict*: of the incidents it acts on, how many carried a flagged conflict. "
        "*Acts on ambiguous*: how many of the genuinely ambiguous scenarios it went on to act on (the right answer is none). "
        "*ECE / Brier*: how far the confidence is from the true hit rate (0 is perfectly calibrated).",
        "",
        "## A. The labelled scenarios",
        "",
        f"{labelled['labelled']} labelled cases (of {labelled['cases']}; the rest are genuinely ambiguous). Hand-written by the Coordinator's "
        "authors: this shows misbehaviour, not general quality.",
        "",
        *table(labelled["per_method"]),
        "",
        "Misses on the labelled cases:",
        "",
    ]
    for method, misses in labelled["misses"].items():
        out.append(f"- `{method}`: " + ("none" if not misses else "; ".join(misses)))
    ambiguous = labelled["per_method"]["authoritative"]
    out += ["", f"Acted on a genuinely ambiguous case (authoritative): {ambiguous['ambiguous_acted_on']} of {ambiguous['ambiguous_n']}.", ""]

    out += [
        "## B. Simulation",
        "",
        "Condition = agent skill / how errors are distributed / relevance model. Agents answer correctly with probability "
        "`skill x relevance` for the true cause, otherwise abstain (60%) or answer wrongly and overconfidently; reported confidence "
        "is 0.65-0.95 when right and 0.50-0.85 when wrong, so confidence is only weakly informative. *shared* errors mean a wrong "
        "answer is the same wrong cause as the other agents' 60% of the time (overlapping evidence). *flat* relevance gives every "
        "agent 0.8 for every cause, so the result does not depend on the proposed relevance weights that P2's fusion also uses.",
        "",
        "**This is a simulation of the inputs, not measured accuracy of the real agents.**",
        "",
        "### What the numbers say",
        "",
        *[f"- {line}" for line in findings(simulation)],
        "",
    ]
    for name, per in simulation.items():
        out += [f"### `{name}`", "", *table(per), ""]

    out += [
        "## Limits",
        "",
        "- The labelled scenarios are few and author-written; the simulation's agent model is an assumption, not data.",
        "- Real agents' errors are neither independent nor as simple as this; the live fault run (`scripts/evaluate_end_to_end.py`) is the "
        "check on real behaviour, and its first result was wrong where this model would have predicted a right answer.",
        "- `noisy_or` shares its relevance weights with the simulation under the *proposed* model; read it under *flat* for a fairer comparison.",
        "",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=500, help="simulated incidents per condition (default 500)")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--out", type=Path, default=REPO / "results" / "coordinator_eval", help="output path without extension")
    args = parser.parse_args(argv)
    if args.n < 1:
        parser.error("--n must be at least 1")

    labelled = run_labelled()
    simulation = run_simulation(args.n, args.seed)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix(".json").write_text(
        json.dumps({"generated_at": generated_at, "n": args.n, "seed": args.seed, "labelled": labelled, "simulation": simulation}, indent=2) + "\n",
        encoding="utf-8",
    )
    args.out.with_suffix(".md").write_text(render_markdown(labelled, simulation, args.n, args.seed, generated_at), encoding="utf-8")

    auth = labelled["per_method"]["authoritative"]
    print(f"labelled scenarios: authoritative accuracy {pct(auth['accuracy'])} on {auth['n']} cases")
    for line in findings(simulation):
        print(f"  {line}")
    print(f"wrote {args.out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
