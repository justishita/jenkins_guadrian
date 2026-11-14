"""Week 8: evaluate the Code & Remediation agent and the policy gate.

**Owner:** P3. Runs every case in ``scenarios/remediation_eval/cases.yaml`` through the
real pipeline - CrewAI code agent, Google ADK Coordinator, remediation engine, and the
actual Rego in ``policies/`` through the opa CLI - and scores it against ground truth:

* **Root cause:** did the code agent name the right failure type, and did it abstain
  when the commit does not explain the failure (TC-12, TC-14)?
* **Remediation:** was a fix proposed exactly when one should be, and was it exactly
  the minimal edit (the inverse of the breaking line)?
* **Safety:** does the remediation policy block every unsafe proposal (TC-15) and pass
  every safe one, does the pull-request policy demand a named human and a draft, and
  does the gate fail closed when OPA cannot answer?
* **Secrets (TC-20):** a token committed in the diff must not appear in any evidence,
  audit record, report or fix.
* **Calibration:** expected calibration error and Brier score of the agent's confidence.

The agent is compared with a naive baseline that blames the latest commit for every
failure and reverts all of it - the H2 baseline from the research question.

    python scripts/get_opa.py            # once
    python scripts/evaluate_remediation.py

Writes ``results/remediation_eval.json`` and ``results/remediation_eval.md``. Exits
non-zero only if the run itself fails; the scores are reported, not asserted.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
import yaml  # noqa: E402

# Importing the demo also quiets third-party logging and makes the console UTF-8 safe.
from scripts.demo_p3 import find_opa, recorded  # noqa: E402
from agents.code_agent.agent import CodeInvestigationAgent  # noqa: E402
from agents.code_agent.remediation import FixPlan, NoFix, propose_fix  # noqa: E402
from agents.code_agent.tools.github_client import GitHubClient  # noqa: E402
from common.audit import FileAuditLog  # noqa: E402
from common.evidence_store import FileEvidenceStore  # noqa: E402
from common.models import Evidence, FailureTaxonomy, IncidentCreatedEvent  # noqa: E402
from coordinator.agent import CoordinatorAgent  # noqa: E402
from coordinator.policy import OpaCliGate, PolicyError  # noqa: E402


CASES = REPO_ROOT / "scenarios" / "remediation_eval" / "cases.yaml"
RESULTS = REPO_ROOT / "results"
REPO = "jenkinsguardians/target-app"
BASELINE_CONFIDENCE = 0.9
CALIBRATION_BINS = 5

# Changes made to the agent after an evaluation run, and what they were measured
# against. Kept in the report so a score is never quoted without its history.
TUNING_LOG = (
    (
        "2026-11-13",
        "E09: `replicas: 2 -> 0` was diagnosed as a configuration error but not reverted, because "
        "`replicas` was not treated as a positive quantity. Added replicas, instances, threads and pool "
        "to POSITIVE_QUANTITY_KEY_RE in agents/code_agent/reasoning.py, with unit tests.",
        "fix recall 87.5% -> 100.0%; E09 confidence 0.70 -> 0.90, so ECE 0.220 -> 0.207 and Brier 0.063 -> 0.058",
    ),
)


# --- running one code case ----------------------------------------------------


def _github(sha: str, case: dict[str, Any]) -> GitHubClient:
    payload = {
        "sha": sha,
        "html_url": f"https://github.com/{REPO}/commit/{sha}",
        "commit": {"message": case["message"], "author": {"name": "eval", "date": "2026-11-13T09:30:00Z"}},
        "files": [
            {
                "filename": f["path"],
                "status": f.get("status", "modified"),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "patch": f.get("patch", ""),
            }
            for f in case.get("files") or []
        ],
    }
    routes = {
        f"/repos/{REPO}/commits/{sha}": payload,
        f"/repos/{REPO}/commits/{sha}/pulls": [],
        f"/repos/{REPO}/commits": [payload],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=routes.get(request.url.path, []))

    return GitHubClient(REPO, "eval-token", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=0)


def _edits(plan: FixPlan | NoFix | None) -> list[tuple[str, str, str]]:
    if not isinstance(plan, FixPlan):
        return []
    return sorted((e.path, e.before, e.after) for e in plan.edits)


def _expected_edits(case: dict[str, Any]) -> list[tuple[str, str, str]]:
    fix = case["expected"]["fix"]
    if fix == "none":
        return []
    return sorted((e["path"], e["before"], e["after"]) for e in fix)


@dataclass
class CodeResult:
    id: str
    tc: str
    name: str
    expected_type: str
    predicted_type: str
    status: str
    confidence: float
    correct: bool
    expected_fix: bool
    proposed_fix: bool
    fix_exact: bool
    fix_reason: str
    policy_allowed: bool | None
    report_type: str
    report_actionable: bool
    secret_leaked: bool | None
    agent_ms: float
    pipeline_ms: float
    baseline_type: str
    baseline_correct: bool
    baseline_fix: bool
    baseline_fix_exact: bool
    notes: list[str] = field(default_factory=list)


async def run_code_case(case: dict[str, Any], opa: OpaCliGate) -> CodeResult:
    incident_id = uuid5(NAMESPACE_URL, f"jenkinsguardians/remediation-eval/{case['id']}")
    sha = uuid5(NAMESPACE_URL, case["id"]).hex + "abcd1234"
    sha = sha[:40]
    event = IncidentCreatedEvent(
        incident_id=incident_id,
        job_name="target-app/main",
        build_number=1,
        build_url="http://jenkins:8080/job/target-app/job/main/1/",
        branch="main",
        git_commit=sha,
        failed_stage=case.get("stage"),
        timestamp=datetime(2026, 11, 13, 9, 31, tzinfo=timezone.utc),
    )
    expected = case["expected"]

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        store = FileEvidenceStore(work / "evidence")
        audit = FileAuditLog(work / "audit")
        github = _github(sha, case)

        started = time.perf_counter()
        evidence = await CodeInvestigationAgent(github, store, audit).run_event(event)
        agent_ms = (time.perf_counter() - started) * 1000

        # The other two agents' evidence is recorded, as in the demo; see cases.yaml.
        jenkins = case.get("jenkins") or {"failure_type": "unknown", "confidence": 0.2, "status": "insufficient_evidence"}
        metrics = case.get("metrics") or {"failure_type": "unknown", "confidence": 0.2, "status": "insufficient_evidence"}
        for agent, spec in (("jenkins_agent", jenkins), ("metrics_agent", metrics)):
            await store.write(
                recorded(
                    agent,
                    incident_id,
                    FailureTaxonomy(spec["failure_type"]),
                    float(spec["confidence"]),
                    f"Recorded {agent} evidence for {case['id']}",
                    status=spec.get("status", "completed"),
                    finding=f"{agent} finding for {case['id']}" if spec["failure_type"] != "unknown" else "",
                )
            )

        report = await CoordinatorAgent(store).synthesize(incident_id)
        _, files = await github.get_commit(sha)
        plan = propose_fix(report, files, base_ref=sha) if report is not None else None

        policy_allowed: bool | None = None
        if isinstance(plan, FixPlan):
            policy_allowed = (await opa.evaluate_remediation(plan.to_policy_input())).allowed
        pipeline_ms = (time.perf_counter() - started) * 1000

        secret = expected.get("secret")
        leaked: bool | None = None
        if secret:
            outputs = [evidence.model_dump_json()]
            outputs += [path.read_text(encoding="utf-8") for path in (work / "audit").rglob("*") if path.is_file()]
            outputs += [path.read_text(encoding="utf-8") for path in (work / "evidence").rglob("*") if path.is_file()]
            if report is not None:
                outputs += [report.model_dump_json(), report.to_markdown()]
            if isinstance(plan, FixPlan):
                outputs += [plan.model_dump_json(), plan.to_markdown()]
            leaked = any(secret in text for text in outputs)

    predicted = evidence.failure_type.value
    abstained = predicted == FailureTaxonomy.UNKNOWN.value or evidence.status == "insufficient_evidence"
    correct = abstained if expected["failure_type"] == "unknown" else (predicted == expected["failure_type"] and not abstained)
    expected_fix = expected["fix"] != "none"
    baseline_type, baseline_edits = blame_latest_commit(case)

    return CodeResult(
        id=case["id"],
        tc=case.get("tc", ""),
        name=case["name"],
        expected_type=expected["failure_type"],
        predicted_type=predicted,
        status=evidence.status,
        confidence=float(evidence.confidence),
        correct=correct,
        expected_fix=expected_fix,
        proposed_fix=isinstance(plan, FixPlan),
        fix_exact=_edits(plan) == _expected_edits(case),
        fix_reason=plan.reason if isinstance(plan, NoFix) else "",
        policy_allowed=policy_allowed,
        report_type=report.failure_type.value if report else "none",
        report_actionable=bool(report and report.actionable),
        secret_leaked=leaked,
        agent_ms=round(agent_ms, 1),
        pipeline_ms=round(pipeline_ms, 1),
        baseline_type=baseline_type,
        baseline_correct=baseline_type == expected["failure_type"],
        baseline_fix=bool(baseline_edits),
        baseline_fix_exact=baseline_edits == _expected_edits(case),
    )


# --- the naive baseline -------------------------------------------------------


def blame_latest_commit(case: dict[str, Any]) -> tuple[str, list[tuple[str, str, str]]]:
    """Blame the most recent commit, always, and revert every line it changed.

    The failure type follows the first changed file (a manifest means a dependency
    regression, a settings or deploy file a configuration error, anything else a
    test failure); an empty commit is still blamed. It never abstains.
    """
    from agents.code_agent.diff_analysis import FileRole, classify_path

    files = case.get("files") or []
    if not files:
        return FailureTaxonomy.CODE_TEST_FAILURE.value, []
    role = classify_path(files[0]["path"])
    if role is FileRole.DEPENDENCY_MANIFEST:
        kind = FailureTaxonomy.DEPENDENCY_REGRESSION.value
    elif role in (FileRole.CONFIGURATION, FileRole.DEPLOYMENT):
        kind = FailureTaxonomy.CONFIG_ERROR.value
    else:
        kind = FailureTaxonomy.CODE_TEST_FAILURE.value

    edits: list[tuple[str, str, str]] = []
    for f in files:
        lines = (f.get("patch") or "").splitlines()
        removed = [line[1:] for line in lines if line.startswith("-")]
        added = [line[1:] for line in lines if line.startswith("+")]
        for index in range(max(len(removed), len(added))):
            before = added[index] if index < len(added) else ""
            after = removed[index] if index < len(removed) else ""
            edits.append((f["path"], before, after))
    return kind, sorted(edits)


# --- policy cases -------------------------------------------------------------


async def run_policy_cases(cases: list[dict[str, Any]], opa: OpaCliGate, *, pull_request: bool) -> list[dict[str, Any]]:
    results = []
    for case in cases:
        if pull_request:
            verdict = await opa.evaluate_pull_request(case["request"])
        else:
            document: dict[str, Any] = {
                "incident_id": "00000000-0000-4000-8000-000000000000",
                "failure_type": "dependency_regression",
                "confidence": case["confidence"],
                "attempt": 0,
                "changed_files": [
                    {
                        "path": f["path"],
                        "status": f.get("status", "modified"),
                        "additions": f.get("additions", 0),
                        "deletions": f.get("deletions", 0),
                        "patch": f.get("patch", ""),
                    }
                    for f in case["files"]
                ],
            }
            if "validation" in case:
                document["validation"] = case["validation"]
            verdict = await opa.evaluate_remediation(document)
        decided = "allow" if verdict.allowed else "deny"
        results.append(
            {
                "id": case["id"],
                "tc": case.get("tc", ""),
                "name": case["name"],
                "expected": case["expected"],
                "decided": decided,
                "correct": decided == case["expected"],
                "reasons": list(verdict.violations),
            }
        )
    return results


async def fails_closed(policy_dir: str) -> bool:
    """With no reachable policy engine, the gate must refuse, not wave the fix through."""
    gate = OpaCliGate(str(REPO_ROOT / ".bin" / "no-such-opa"), policy_dir, timeout_seconds=5)
    try:
        verdict = await gate.evaluate_remediation({"changed_files": [], "confidence": 1.0})
    except PolicyError:
        return True
    return not verdict.allowed


# --- metrics ------------------------------------------------------------------


def calibration(results: list[CodeResult]) -> dict[str, float]:
    """Expected calibration error and Brier score of the agent's confidence.

    The confidence of an abstention is its confidence that a code change caused the
    failure, so it is scored against "the commit was the cause".
    """
    pairs: list[tuple[float, int]] = []
    for r in results:
        if r.expected_type == "unknown":
            # Confidence here is that the commit caused it; the truth is that it did not.
            pairs.append((r.confidence, 0))
        else:
            pairs.append((r.confidence, int(r.correct)))
    brier = statistics.fmean((p - y) ** 2 for p, y in pairs)
    ece = 0.0
    for b in range(CALIBRATION_BINS):
        low, high = b / CALIBRATION_BINS, (b + 1) / CALIBRATION_BINS
        members = [(p, y) for p, y in pairs if low <= p < high or (b == CALIBRATION_BINS - 1 and p == 1.0)]
        if members:
            gap = abs(statistics.fmean(p for p, _ in members) - statistics.fmean(y for _, y in members))
            ece += len(members) / len(pairs) * gap
    return {"ece": round(ece, 4), "brier": round(brier, 4)}


def rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def summarize(code: list[CodeResult], remediation: list[dict[str, Any]], pull: list[dict[str, Any]], closed: bool) -> dict[str, Any]:
    should_abstain = [r for r in code if r.expected_type == "unknown"]
    should_name = [r for r in code if r.expected_type != "unknown"]
    should_fix = [r for r in code if r.expected_fix]
    should_not_fix = [r for r in code if not r.expected_fix]
    proposed = [r for r in code if r.proposed_fix]
    unsafe = [c for c in remediation if c["expected"] == "deny"]
    safe = [c for c in remediation if c["expected"] == "allow"]
    secrets = [r for r in code if r.secret_leaked is not None]
    return {
        "cases": len(code),
        "agent": {
            "root_cause_accuracy": rate(sum(r.correct for r in code), len(code)),
            "named_cause_accuracy": rate(sum(r.correct for r in should_name), len(should_name)),
            "abstention_rate_when_commit_innocent": rate(sum(r.correct for r in should_abstain), len(should_abstain)),
            "false_blame_rate": rate(sum(not r.correct for r in should_abstain), len(should_abstain)),
            "fix_recall": rate(sum(r.proposed_fix and r.fix_exact for r in should_fix), len(should_fix)),
            "fix_precision": rate(sum(r.fix_exact for r in proposed), len(proposed)),
            "unnecessary_fix_rate": rate(sum(r.proposed_fix for r in should_not_fix), len(should_not_fix)),
            "generated_fixes_allowed_by_policy": rate(sum(bool(r.policy_allowed) for r in proposed), len(proposed)),
            "avg_confidence_correct": round(statistics.fmean(r.confidence for r in code if r.correct), 3) if any(r.correct for r in code) else None,
            "avg_confidence_incorrect": round(statistics.fmean(r.confidence for r in code if not r.correct), 3) if any(not r.correct for r in code) else None,
            **calibration(code),
            "agent_ms_p50": round(statistics.median(r.agent_ms for r in code), 1),
            "pipeline_ms_p50": round(statistics.median(r.pipeline_ms for r in code), 1),
            "pipeline_ms_max": round(max(r.pipeline_ms for r in code), 1),
        },
        "baseline": {
            "root_cause_accuracy": rate(sum(r.baseline_correct for r in code), len(code)),
            "named_cause_accuracy": rate(sum(r.baseline_correct for r in should_name), len(should_name)),
            "abstention_rate_when_commit_innocent": 0.0 if should_abstain else None,
            "false_blame_rate": 1.0 if should_abstain else None,
            "fix_recall": rate(sum(r.baseline_fix and r.baseline_fix_exact for r in should_fix), len(should_fix)),
            "fix_precision": rate(
                sum(r.baseline_fix_exact for r in code if r.baseline_fix), sum(r.baseline_fix for r in code)
            ),
            "unnecessary_fix_rate": rate(sum(r.baseline_fix for r in should_not_fix), len(should_not_fix)),
        },
        "policy": {
            "remediation_cases": len(remediation),
            "unsafe_blocked": rate(sum(c["correct"] for c in unsafe), len(unsafe)),
            "safe_allowed": rate(sum(c["correct"] for c in safe), len(safe)),
            "remediation_accuracy": rate(sum(c["correct"] for c in remediation), len(remediation)),
            "pull_request_cases": len(pull),
            "pull_request_accuracy": rate(sum(c["correct"] for c in pull), len(pull)),
            "fails_closed_without_opa": closed,
        },
        "secrets": {
            "cases": len(secrets),
            "leaks": sum(bool(r.secret_leaked) for r in secrets),
        },
    }


# --- report -------------------------------------------------------------------


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def markdown(summary: dict[str, Any], code: list[CodeResult], remediation: list[dict[str, Any]], pull: list[dict[str, Any]], generated: str) -> str:
    a, b, p, s = summary["agent"], summary["baseline"], summary["policy"], summary["secrets"]
    lines = [
        "# Code & Remediation Agent Evaluation Results",
        "",
        f"Generated: {generated}  ",
        f"Cases: {summary['cases']} commits, {p['remediation_cases']} remediation proposals, "
        f"{p['pull_request_cases']} pull-request requests  ",
        "Command: `python scripts/evaluate_remediation.py`",
        "",
        "## Agent versus the naive baseline",
        "",
        "The baseline blames the latest commit for every failure and reverts all of it.",
        "",
        "| Metric | Code agent + remediation | Blame the latest commit |",
        "|--------|-------------|----------|",
        f"| Root-cause accuracy (all cases) | {pct(a['root_cause_accuracy'])} | {pct(b['root_cause_accuracy'])} |",
        f"| Named the right cause when the commit was guilty | {pct(a['named_cause_accuracy'])} | {pct(b['named_cause_accuracy'])} |",
        f"| Abstained when the commit was innocent (TC-12, TC-14) | {pct(a['abstention_rate_when_commit_innocent'])} | {pct(b['abstention_rate_when_commit_innocent'])} |",
        f"| False-blame rate | {pct(a['false_blame_rate'])} | {pct(b['false_blame_rate'])} |",
        f"| Fix recall (exact minimal fix when one exists) | {pct(a['fix_recall'])} | {pct(b['fix_recall'])} |",
        f"| Fix precision (proposed fixes that are exactly right) | {pct(a['fix_precision'])} | {pct(b['fix_precision'])} |",
        f"| Unnecessary-fix rate (fix proposed when none should be) | {pct(a['unnecessary_fix_rate'])} | {pct(b['unnecessary_fix_rate'])} |",
        "",
        "## Agent detail",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Generated fixes allowed by the remediation policy | {pct(a['generated_fixes_allowed_by_policy'])} |",
        f"| Avg confidence when correct | {a['avg_confidence_correct'] if a['avg_confidence_correct'] is not None else 'n/a'} |",
        f"| Avg confidence when incorrect | {a['avg_confidence_incorrect'] if a['avg_confidence_incorrect'] is not None else 'n/a (no incorrect cases)'} |",
        f"| ECE ({CALIBRATION_BINS} bins) | {a['ece']} |",
        f"| Brier score | {a['brier']} |",
        f"| Code agent time, P50 | {a['agent_ms_p50']} ms |",
        f"| Full pipeline (agent, fusion, fix, OPA), P50 / max | {a['pipeline_ms_p50']} / {a['pipeline_ms_max']} ms |",
        "",
        "## Safety",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Unsafe remediations blocked (TC-15 and guardrails) | {pct(p['unsafe_blocked'])} |",
        f"| Safe remediations allowed | {pct(p['safe_allowed'])} |",
        f"| Pull-request policy decisions correct | {pct(p['pull_request_accuracy'])} |",
        f"| Gate fails closed when OPA is unavailable | {'yes' if p['fails_closed_without_opa'] else 'NO'} |",
        f"| Secret leaks across evidence, audit, report and fix (TC-20) | {s['leaks']} of {s['cases']} case(s) |",
        "",
        "## Per case",
        "",
        "| Case | TC | Scenario | Expected | Agent said | Conf. | Right | Fix expected | Fix proposed | Exact | Baseline |",
        "|------|----|----------|----------|-----------|-------|-------|--------------|--------------|-------|----------|",
    ]
    for r in code:
        lines.append(
            f"| {r.id} | {r.tc} | {r.name} | {r.expected_type} | {r.predicted_type} ({r.status}) | "
            f"{r.confidence:.2f} | {'yes' if r.correct else '**no**'} | {'yes' if r.expected_fix else 'no'} | "
            f"{'yes' if r.proposed_fix else 'no'} | {'yes' if r.fix_exact else '**no**'} | "
            f"{r.baseline_type}{'' if r.baseline_correct else ' (wrong)'} |"
        )
    lines += ["", "## Remediation policy", "", "| Case | TC | Proposal | Expected | Decided | Reason |", "|------|----|----------|----------|---------|--------|"]
    for c in remediation:
        reason = c["reasons"][0] if c["reasons"] else ""
        lines.append(f"| {c['id']} | {c['tc']} | {c['name']} | {c['expected']} | {c['decided']}{'' if c['correct'] else ' **WRONG**'} | {reason} |")
    lines += ["", "## Pull-request policy", "", "| Case | TC | Request | Expected | Decided |", "|------|----|---------|----------|---------|"]
    for c in pull:
        lines.append(f"| {c['id']} | {c['tc']} | {c['name']} | {c['expected']} | {c['decided']}{'' if c['correct'] else ' **WRONG**'} |")
    misses = [r for r in code if not r.correct or not r.fix_exact]
    lines += ["", "## Where it fell short", ""]
    if not misses:
        lines.append("Every case met its ground truth.")
    for r in misses:
        if not r.correct:
            lines.append(f"* **{r.id} {r.name}:** expected {r.expected_type}, the agent said {r.predicted_type} at {r.confidence:.2f}.")
        elif r.expected_fix and not r.proposed_fix:
            lines.append(f"* **{r.id} {r.name}:** right cause, but no fix was proposed: {r.fix_reason}")
        else:
            lines.append(f"* **{r.id} {r.name}:** right cause, but the fix was not the expected minimal edit.")
    lines += ["", "## Tuning log", "", "| Date | Change | Effect |", "|------|--------|--------|"]
    lines += [f"| {date} | {change} | {effect} |" for date, change, effect in TUNING_LOG]
    lines += ["", "Re-running after a change is the only way a score here moves; the cases are never edited to fit."]
    lines += [
        "",
        "## Threats to validity",
        "",
        "* The cases were written by the author of the rules, so a perfect score mostly shows consistency, not generality.",
        "* The Jenkins and metrics evidence is recorded, not live; fusion is real, its inputs are not.",
        "* 15 commits is a small sample; one case moves a rate by about 7 points.",
        "* The one tuning change was made after seeing case E09, so E09 is no longer an unseen case.",
        "* Calibration is measured on 15 points; ECE with 5 bins is coarse at this size.",
        "* The baseline is deliberately naive. It is the strawman H2 names, not a tuned competitor.",
        "",
        "> To add a case, append it to `scenarios/remediation_eval/cases.yaml` and re-run.",
        "",
    ]
    return "\n".join(lines)


async def evaluate(opa_binary: str) -> dict[str, Any]:
    data = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    policy_dir = str(REPO_ROOT / "policies")
    opa = OpaCliGate(opa_binary, policy_dir)
    code = [await run_code_case(case, opa) for case in data["code_cases"]]
    remediation = await run_policy_cases(data["remediation_policy_cases"], opa, pull_request=False)
    pull = await run_policy_cases(data["pull_request_policy_cases"], opa, pull_request=True)
    closed = await fails_closed(policy_dir)
    generated = datetime.now(timezone.utc).isoformat()
    summary = summarize(code, remediation, pull, closed)
    return {
        "generated": generated,
        "summary": summary,
        "code_cases": [asdict(r) for r in code],
        "remediation_policy_cases": remediation,
        "pull_request_policy_cases": pull,
        "markdown": markdown(summary, code, remediation, pull, generated),
    }


def main() -> int:
    opa = find_opa()
    if opa is None:
        print("opa not found. Run:  python scripts/get_opa.py", file=sys.stderr)
        return 2
    result = asyncio.run(evaluate(opa))
    RESULTS.mkdir(exist_ok=True)
    report = result.pop("markdown")
    (RESULTS / "remediation_eval.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (RESULTS / "remediation_eval.md").write_text(report, encoding="utf-8")
    a, p = result["summary"]["agent"], result["summary"]["policy"]
    print(f"root cause {pct(a['root_cause_accuracy'])} | false blame {pct(a['false_blame_rate'])} | "
          f"fix recall {pct(a['fix_recall'])} | fix precision {pct(a['fix_precision'])} | "
          f"unsafe blocked {pct(p['unsafe_blocked'])} | PR policy {pct(p['pull_request_accuracy'])} | "
          f"fails closed {p['fails_closed_without_opa']} | secret leaks {result['summary']['secrets']['leaks']}")
    print(f"wrote {RESULTS / 'remediation_eval.md'}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
