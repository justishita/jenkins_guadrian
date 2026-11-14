"""Week 8: end-to-end accuracy on real injected faults.

**Owner:** P2. Every fault pushed by ``scripts/inject_fault.py`` (branch ``fault/<scenario>-<timestamp>``) goes
through the real stack: Jenkins builds it, the webhook opens an incident, the three agents investigate and the
Coordinator writes a report. This script finds those incidents, reads what each agent concluded and what the
Coordinator decided (``data/reports``), and scores them against the injector's ground truth:

    tc01_unit_test       -> code_test_failure
    tc02_compile_syntax  -> build_compilation_failure
    tc02_compile_import  -> dependency_regression
    tc10_flaky           -> flaky_test

It reports, for the Coordinator and for each agent separately, accuracy with a 95% Wilson interval (the samples are
few, so a bare percentage would overstate what is known), every miss in full, where each miss was lost (no agent was
right, or an agent was right and the fusion overruled it), and the misses that were *actionable*: a wrong verdict the
remediation engine would have acted on is the dangerous kind.

    python scripts/evaluate_end_to_end.py                  # every fault incident in the Docker stack
    python scripts/evaluate_end_to_end.py --since 2026-11-16
    python scripts/evaluate_end_to_end.py --incidents-file incidents.json   # no Docker (a list of incident rows)

Reads the incident list from the stack's PostgreSQL through ``docker compose exec`` and the reports from
``data/reports``. Writes ``results/e2e_accuracy.json`` and ``results/e2e_accuracy.md``. The scores are reported, not
asserted: a miss is a finding, not a failure of the run.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import unquote

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from backend.ui.reports import derive, summarize  # noqa: E402  (the same derivation the UI shows)

#: What each injected fault really is (``scripts/inject_fault.py`` writes the same into its ground-truth file).
EXPECTED = {
    "tc01_unit_test": "code_test_failure",
    "tc02_compile_syntax": "build_compilation_failure",
    "tc02_compile_import": "dependency_regression",
    "tc10_flaky": "flaky_test",
}
AGENTS = ("jenkins_agent", "metrics_agent", "code_agent")
BRANCH = re.compile(r"^fault/(?P<scenario>.+)-\d{8}T\d+Z$")
INCIDENT_SQL = (
    "SELECT id, coalesce(branch, ''), job_name, coalesce(build_number, 0), created_at "
    "FROM incidents WHERE branch LIKE 'fault/%' OR job_name LIKE '%fault%2F%' ORDER BY created_at"
)


@dataclass
class AgentFinding:
    failure_type: str | None
    confidence: float | None
    abstained: bool
    correct: bool | None  # None when the agent abstained or is missing
    summary: str = ""


@dataclass
class Sample:
    incident_id: str
    scenario: str
    expected: str
    branch: str
    build: int
    created_at: str
    status: str  # scored | pending (no report yet) | unknown_scenario
    verdict: str | None = None
    confidence: float | None = None
    correct: bool | None = None
    complete: bool | None = None
    conflicted: bool | None = None
    actionable: bool | None = None
    revised_to: str | None = None  # the verdict of a later report revision, if it changed
    agents: dict[str, AgentFinding] = field(default_factory=dict)

    @property
    def short(self) -> str:
        return self.incident_id[:8]


# ---------------------------------------------------------------------------------------------- reading the stack


def parse_incident_rows(text: str) -> list[dict[str, Any]]:
    """Rows from ``psql -tA -F '|'``: id | branch | job_name | build | created_at."""
    rows = []
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 5 or not parts[0]:
            continue
        rows.append({"id": parts[0], "branch": parts[1], "job_name": parts[2], "build": int(parts[3] or 0), "created_at": parts[4]})
    return rows


def read_incidents_from_stack() -> list[dict[str, Any]]:
    command = 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -F "|" -c "' + INCIDENT_SQL.replace('"', '\\"') + '"'
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "sh", "-c", command],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not read incidents from the stack: {result.stderr.strip()[:300]}")
    return parse_incident_rows(result.stdout)


def branch_of(row: dict[str, Any]) -> str:
    if row.get("branch"):
        return str(row["branch"])
    job = str(row.get("job_name", ""))
    return unquote(job.split("/", 1)[1]) if "/" in job else job  # target-app/fault%2Ftc01...


def scenario_of(branch: str) -> str | None:
    match = BRANCH.match(branch)
    return match.group("scenario") if match else None


def read_envelopes(reports_dir: Path, incident_id: str) -> list[dict[str, Any]]:
    """Report revisions of the *first* attempt, oldest first (later attempts are re-investigations after a fix)."""
    folder = reports_dir / incident_id
    envelopes = []
    for path in sorted(folder.glob("a0-r*.json")) if folder.is_dir() else []:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            document["report"].update(derive(document["report"]))  # complete / actionable are not stored in the file
        except (OSError, ValueError, KeyError, TypeError):
            continue
        envelopes.append(document)
    return sorted(envelopes, key=lambda e: e.get("revision") or 0)


def build_sample(row: dict[str, Any], reports_dir: Path) -> Sample:
    branch = branch_of(row)
    scenario = scenario_of(branch) or "unknown"
    expected = EXPECTED.get(scenario, "")
    sample = Sample(str(row["id"]), scenario, expected, branch, int(row.get("build") or 0), str(row.get("created_at", "")), "scored")
    if not expected:
        sample.status = "unknown_scenario"
        return sample
    envelopes = read_envelopes(reports_dir, sample.incident_id)
    if not envelopes:
        sample.status = "pending"
        return sample

    initial, latest = envelopes[0], envelopes[-1]
    shown = summarize(initial) or {}
    sample.verdict = shown.get("verdict")
    sample.confidence = shown.get("confidence")
    sample.correct = sample.verdict == expected
    sample.complete = bool(shown.get("complete"))
    sample.conflicted = bool(shown.get("conflicted"))
    sample.actionable = bool(shown.get("actionable"))
    final = (summarize(latest) or {}).get("verdict")
    if final != sample.verdict:
        sample.revised_to = final
    for finding in (initial.get("report") or {}).get("findings") or []:
        agent = str(finding.get("agent"))
        abstained = bool(finding.get("abstained")) or finding.get("failure_type") in (None, "unknown")
        sample.agents[agent] = AgentFinding(
            failure_type=finding.get("failure_type"),
            confidence=finding.get("confidence"),
            abstained=abstained,
            correct=None if abstained else finding.get("failure_type") == expected,
            summary=str(finding.get("summary", ""))[:180],
        )
    return sample


# ------------------------------------------------------------------------------------------------------- scoring


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval: honest about how little a handful of samples shows."""
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate(successes: int, total: int) -> dict[str, Any]:
    low, high = wilson(successes, total)
    return {"correct": successes, "n": total, "accuracy": (successes / total) if total else None, "ci95": [round(low, 3), round(high, 3)]}


def score(samples: list[Sample]) -> dict[str, Any]:
    scored = [s for s in samples if s.status == "scored"]
    per_scenario: dict[str, dict[str, Any]] = {}
    for scenario in EXPECTED:
        group = [s for s in scored if s.scenario == scenario]
        per_scenario[scenario] = {
            "expected": EXPECTED[scenario],
            **rate(sum(1 for s in group if s.correct), len(group)),
            "verdicts": dict(Counter(s.verdict for s in group)),
        }

    per_agent: dict[str, dict[str, Any]] = {}
    for agent in AGENTS:
        findings = [s.agents[agent] for s in scored if agent in s.agents]
        answered = [f for f in findings if not f.abstained]
        per_agent[agent] = {
            "incidents": len(scored),
            "reported": len(findings),
            "abstained": sum(1 for f in findings if f.abstained),
            "missing": len(scored) - len(findings),
            "answered": rate(sum(1 for f in answered if f.correct), len(answered)),
            "overall": rate(sum(1 for f in answered if f.correct), len(scored)),  # abstaining counts as not right
        }

    wrong = [s for s in scored if not s.correct]
    overruled = [s for s in wrong if any(f.correct for f in s.agents.values())]
    nobody = [s for s in wrong if not any(f.correct for f in s.agents.values())]
    right = [s for s in scored if s.correct]

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 3) if values else None

    return {
        "scored": len(scored),
        "pending": [s.short for s in samples if s.status == "pending"],
        "unknown_scenario": [s.short for s in samples if s.status == "unknown_scenario"],
        "coordinator": rate(len(right), len(scored)),
        "per_scenario": per_scenario,
        "per_agent": per_agent,
        "misses": {
            "total": len(wrong),
            "agent_was_right_but_overruled": len(overruled),
            "no_agent_was_right": len(nobody),
            "wrong_and_actionable": sum(1 for s in wrong if s.actionable),
            "wrong_and_conflict_flagged": sum(1 for s in wrong if s.conflicted),
        },
        "confidence": {"mean_when_right": mean([s.confidence for s in right if s.confidence is not None]),
                       "mean_when_wrong": mean([s.confidence for s in wrong if s.confidence is not None])},
        "any_agent_right": rate(sum(1 for s in scored if any(f.correct for f in s.agents.values())), len(scored)),
    }


# ------------------------------------------------------------------------------------------------------ reporting


def pct(item: dict[str, Any]) -> str:
    if not item["n"]:
        return "n/a"
    low, high = item["ci95"]
    return f"{item['correct']}/{item['n']} = {100 * item['accuracy']:.0f}% (95% CI {100 * low:.0f}-{100 * high:.0f}%)"


def render_markdown(samples: list[Sample], result: dict[str, Any], generated_at: str) -> str:
    scored = result["scored"]
    out = [
        "# End-to-end accuracy on injected faults",
        "",
        f"_Generated {generated_at} by `scripts/evaluate_end_to_end.py`. Real faults through the real stack; ground truth from `scripts/inject_fault.py`._",
        "",
        "## Headline",
        "",
        f"- **Coordinator verdict correct: {pct(result['coordinator'])}**",
        f"- At least one agent named the right cause: {pct(result['any_agent_right'])}",
        f"- Incidents scored: {scored}" + (f"; still without a report: {', '.join(result['pending'])}" if result["pending"] else ""),
        "",
    ]
    if scored < 30:
        out += [f"> **Small sample ({scored}).** The interval above is wide on purpose: this shows what went wrong in these runs, "
                "not a reliable accuracy rate. Treat the misses below as the main result.", ""]

    out += ["## By fault type", "", "| Fault | Known cause | Result | Verdicts the Coordinator gave |", "|---|---|---|---|"]
    for scenario, item in result["per_scenario"].items():
        seen = ", ".join(f"{name} x{count}" for name, count in item["verdicts"].items()) or "no incident"
        out.append(f"| `{scenario}` | `{item['expected']}` | {pct(item)} | {seen} |")

    out += ["", "## By agent", "",
            "_\"Answered\" counts only runs where the agent named a cause; \"overall\" counts an abstention as not right._", "",
            "| Agent | Answered right | Overall right | Abstained | Missing |", "|---|---|---|---|---|"]
    for agent, item in result["per_agent"].items():
        out.append(f"| {agent} | {pct(item['answered'])} | {pct(item['overall'])} | {item['abstained']} | {item['missing']} |")

    misses = result["misses"]
    out += ["", "## Where the misses were lost", "",
            f"- Coordinator misses: **{misses['total']}** of {scored}",
            f"- No agent had the right cause: {misses['no_agent_was_right']} (an investigation problem)",
            f"- An agent was right and the fusion overruled it: {misses['agent_was_right_but_overruled']} (a Coordinator problem)",
            f"- Wrong **and actionable** (the remediation engine would have acted on it): **{misses['wrong_and_actionable']}**",
            f"- Wrong but flagged as a conflict: {misses['wrong_and_conflict_flagged']}",
            f"- Mean confidence when right: {result['confidence']['mean_when_right']}; when wrong: {result['confidence']['mean_when_wrong']}"
            " (a useful confidence would be clearly lower when wrong)", ""]

    wrong = [s for s in samples if s.status == "scored" and not s.correct]
    out += ["## Every miss", ""]
    if not wrong:
        out += ["No misses in this run.", ""]
    for s in wrong:
        flags = [name for name, on in (("complete", s.complete), ("conflict", s.conflicted), ("actionable", s.actionable)) if on]
        out += [f"### `{s.short}` {s.scenario} (build {s.build})", "",
                f"- Expected `{s.expected}`; Coordinator said **`{s.verdict}`** ({s.confidence}); flags: {', '.join(flags) or 'none'}"
                + (f"; a later revision changed it to `{s.revised_to}`" if s.revised_to else ""), ""]
        for agent in AGENTS:
            finding = s.agents.get(agent)
            if finding is None:
                out.append(f"  - {agent}: no finding")
                continue
            mark = "abstained" if finding.abstained else ("right" if finding.correct else "WRONG")
            out.append(f"  - {agent}: `{finding.failure_type}` ({finding.confidence}) {mark}: {finding.summary}")
        out.append("")

    out += ["## How to read this", "",
            "- One incident is one sample; a fault branch that builds several times gives several samples.",
            "- Ground truth is the fault the injector introduced, so a build that fails for an unrelated reason would be scored wrong.",
            "- Re-run after any change to an agent, the Coordinator or the prompts; compare the misses, not just the headline.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", help="only incidents created on or after this date (YYYY-MM-DD)")
    parser.add_argument("--reports-dir", type=Path, default=REPO / "data" / "reports")
    parser.add_argument("--incidents-file", type=Path, help="JSON list of {id, branch, job_name, build, created_at}; skips Docker")
    parser.add_argument("--out", type=Path, default=REPO / "results" / "e2e_accuracy", help="output path without extension")
    args = parser.parse_args(argv)

    try:
        rows = json.loads(args.incidents_file.read_text(encoding="utf-8")) if args.incidents_file else read_incidents_from_stack()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"{error}\nIs the stack up? Run: docker compose up -d   (or pass --incidents-file to score without Docker)")
        return 2
    if args.since:
        rows = [r for r in rows if str(r.get("created_at", "")) >= args.since]
    if not rows:
        print("no fault incidents found: inject some with scripts/inject_fault.py first")
        return 1

    samples = [build_sample(row, args.reports_dir) for row in rows]
    result = score(samples)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix(".json").write_text(
        json.dumps({"generated_at": generated_at, "result": result, "samples": [asdict(s) for s in samples]}, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    args.out.with_suffix(".md").write_text(render_markdown(samples, result, generated_at), encoding="utf-8")

    print(f"scored {result['scored']} incident(s); Coordinator {pct(result['coordinator'])}")
    for scenario, item in result["per_scenario"].items():
        print(f"  {scenario:22} {pct(item)}  verdicts: {item['verdicts']}")
    if result["pending"]:
        print(f"  waiting for a report: {', '.join(result['pending'])}")
    print(f"wrote {args.out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
