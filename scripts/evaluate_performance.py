"""Week 8: end-to-end performance evaluation (run time, cost estimate, prompt variants), replayed from recorded data.

**Owner:** P2. Answers "how long does an investigation take, what does it cost, and does the prompt style change either?"
without spending LLM quota. It has four parts, and the report says which numbers are *measured* and which are *estimated*:

  A. Recorded runs   (MEASURED)   every incident in ``data/audit``: each agent's run time, the time from the incident being
                                  opened to the Coordinator's report, tool-call latency. Real runs on the real stack.
  B. Coordinator replay (MEASURED) the recorded evidence of every incident is fed to the real ``synthesize`` several times:
                                  fusion run time, and whether the verdict is the same every time (it must be).
  C. Rule-path replay (MEASURED)  every recorded build log (``tests/jenkins_agent/fixtures``) through the real Jenkins-agent
                                  parser and classifier several times: run time and, for the labelled logs, accuracy.
  E. Dispatch pattern (MEASURED, --stack) the Coordinator runs its three agents in parallel. Each real dispatch time is in the
                                  database, so the time a sequential Coordinator would have taken is the sum, and the parallel
                                  time actually seen is about the slowest agent. Needs the Docker stack (reads PostgreSQL).
  D. Prompt variants (ESTIMATED)  every Jenkins-agent prompt variant (react, plan_execute, three personas) against every build
                                  log: the size of the prompt the agent would send, turned into tokens and dollars with a price
                                  table. **No LLM is called**, so latency and accuracy per variant are NOT measured here; what
                                  differs between variants, and across logs, is how much text goes to the model.

The price table and turn count are assumptions you can change on the command line. Writes ``results/performance_eval.json``,
``results/performance_eval.md``, ``results/performance_runs.csv`` and one SVG box plot per measure (no plotting library needed).

    python scripts/evaluate_performance.py
    python scripts/evaluate_performance.py --repeats 10 --price-in 0.30 --price-out 2.50 --turns 4
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A run is "unfinished" (and excluded) when its end event is far later than its start: the process died and a later
# attempt finished it. Two hours is far above any real investigation here.
MAX_RUN_SECONDS = 2 * 3600
TERMINAL = {"agent_completed", "agent_failed"}
FIXTURES = ROOT / "tests" / "jenkins_agent" / "fixtures"
PROMPTS = ROOT / "agents" / "jenkins_agent" / "prompts"
CHARS_PER_TOKEN = 4  # a rule of thumb for English and logs; the real tokenizer differs a little


# ----------------------------------------------------------------------------------------------------------------------
# statistics


def quantile(values: list[float], q: float) -> float:
    """Linear-interpolated quantile; ``values`` need not be sorted."""
    ordered = sorted(values)
    if not ordered:
        return math.nan
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def describe(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": min(values),
        "p25": quantile(values, 0.25),
        "median": quantile(values, 0.5),
        "p75": quantile(values, 0.75),
        "p95": quantile(values, 0.95),
        "max": max(values),
        "mean": statistics.fmean(values),
        "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
    }


def box_svg(groups: dict[str, list[float]], *, title: str, unit: str, log_scale: bool = False) -> str:
    """A self-contained SVG box plot: box = p25..p75, line = median, whiskers = furthest point within 1.5 IQR, dots = outliers."""
    groups = {name: values for name, values in groups.items() if values}
    width, height, left, right, top, bottom = 760, 380, 70, 20, 40, 90
    plot_w, plot_h = width - left - right, height - top - bottom
    if not groups:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="60"><text x="10" y="30">{title}: no data</text></svg>'

    def transform(value: float) -> float:
        return math.log10(max(value, 1e-6)) if log_scale else value

    every = [transform(v) for values in groups.values() for v in values]
    lo, hi = min(every), max(every)
    if hi == lo:
        hi = lo + 1
    pad = (hi - lo) * 0.06
    lo, hi = lo - pad, hi + pad

    def y(value: float) -> float:
        return top + plot_h - (transform(value) - lo) / (hi - lo) * plot_h

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" font-family="sans-serif" font-size="12">',
        f'<rect width="{width}" height="{height}" fill="white"/>',
        f'<text x="{left}" y="22" font-size="14" font-weight="bold">{title}</text>',
        f'<text x="14" y="{top + plot_h / 2}" transform="rotate(-90 14 {top + plot_h / 2})" text-anchor="middle">{unit}{" (log scale)" if log_scale else ""}</text>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#444"/>',
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#444"/>',
    ]
    for step in range(5):  # five gridlines
        raw = lo + (hi - lo) * step / 4
        value = 10**raw if log_scale else raw
        yy = top + plot_h - plot_h * step / 4
        parts.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{left + plot_w}" y2="{yy:.1f}" stroke="#ddd"/>')
        parts.append(f'<text x="{left - 6}" y="{yy + 4:.1f}" text-anchor="end">{value:.3g}</text>')
    slot = plot_w / len(groups)
    for index, (name, values) in enumerate(groups.items()):
        cx = left + slot * (index + 0.5)
        half = min(slot * 0.28, 40)
        q1, med, q3 = quantile(values, 0.25), quantile(values, 0.5), quantile(values, 0.75)
        iqr = q3 - q1
        inside = [v for v in values if q1 - 1.5 * iqr <= v <= q3 + 1.5 * iqr] or values
        low_w, high_w = min(inside), max(inside)
        parts.append(f'<line x1="{cx:.1f}" y1="{y(low_w):.1f}" x2="{cx:.1f}" y2="{y(q1):.1f}" stroke="#345"/>')
        parts.append(f'<line x1="{cx:.1f}" y1="{y(q3):.1f}" x2="{cx:.1f}" y2="{y(high_w):.1f}" stroke="#345"/>')
        for cap in (low_w, high_w):
            parts.append(f'<line x1="{cx - half / 2:.1f}" y1="{y(cap):.1f}" x2="{cx + half / 2:.1f}" y2="{y(cap):.1f}" stroke="#345"/>')
        box_top, box_bottom = min(y(q1), y(q3)), max(y(q1), y(q3))
        parts.append(
            f'<rect x="{cx - half:.1f}" y="{box_top:.1f}" width="{2 * half:.1f}" height="{max(box_bottom - box_top, 1):.1f}" fill="#8ab4f8" stroke="#345"/>'
        )
        parts.append(f'<line x1="{cx - half:.1f}" y1="{y(med):.1f}" x2="{cx + half:.1f}" y2="{y(med):.1f}" stroke="#c00" stroke-width="2"/>')
        for outlier in (v for v in values if v < low_w or v > high_w):
            parts.append(f'<circle cx="{cx:.1f}" cy="{y(outlier):.1f}" r="2.5" fill="none" stroke="#345"/>')
        label = name if len(name) <= 16 else name[:15] + "…"
        parts.append(f'<text x="{cx:.1f}" y="{top + plot_h + 16}" text-anchor="end" transform="rotate(-30 {cx:.1f} {top + plot_h + 16})">{label}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{top + plot_h + 78}" text-anchor="middle" fill="#666">n={len(values)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


# ----------------------------------------------------------------------------------------------------------------------
# A. recorded runs (measured)


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def read_audit(audit_dir: Path) -> dict[str, list[dict[str, Any]]]:
    incidents: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(audit_dir.glob("*.jsonl")):
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if events:
            incidents[path.stem] = sorted(events, key=lambda event: event["created_at"])
    return incidents


def recorded_runs(audit_dir: Path) -> dict[str, Any]:
    """Run time per agent and per incident from the audit trail of the real stack."""
    agent_seconds: dict[str, list[float]] = {}
    tool_ms: dict[str, list[float]] = {}
    end_to_end: list[float] = []
    rows: list[dict[str, Any]] = []
    unfinished = 0
    incidents = read_audit(audit_dir)
    for incident_id, events in incidents.items():
        open_runs: dict[str, datetime] = {}
        first_seen = _time(events[0]["created_at"])
        for event in events:
            actor, kind, when = event.get("actor", ""), event["event_type"], _time(event["created_at"])
            if kind == "agent_started":
                if actor in open_runs:
                    unfinished += 1  # a start with no end before the next start: the earlier run died
                open_runs[actor] = when
            elif kind in TERMINAL and actor in open_runs:
                seconds = (when - open_runs.pop(actor)).total_seconds()
                if 0 <= seconds <= MAX_RUN_SECONDS:
                    agent_seconds.setdefault(actor, []).append(seconds)
                    rows.append({"part": "recorded_agent_run", "group": actor, "run": incident_id[:8], "value": seconds, "unit": "s"})
                else:
                    unfinished += 1
            elif kind == "tool_call" and event.get("duration_ms") is not None:
                tool_ms.setdefault(actor, []).append(float(event["duration_ms"]))
            elif kind == "synthesis_completed":
                seconds = (when - first_seen).total_seconds()
                if 0 <= seconds <= MAX_RUN_SECONDS and event.get("payload", {}).get("attempt", 0) == 0:
                    end_to_end.append(seconds)
                    rows.append({"part": "recorded_end_to_end", "group": "incident->report", "run": incident_id[:8], "value": seconds, "unit": "s"})
                    first_seen = datetime.max.replace(tzinfo=timezone.utc)  # only the first report counts
        unfinished += len(open_runs)
    return {
        "incidents": len(incidents),
        "agent_seconds": agent_seconds,
        "tool_ms": tool_ms,
        "end_to_end_seconds": end_to_end,
        "unfinished_runs_excluded": unfinished,
        "rows": rows,
    }


# ----------------------------------------------------------------------------------------------------------------------
# B. Coordinator replay (measured)


async def coordinator_replay(evidence_dir: Path, blackboard_dir: Path, repeats: int) -> dict[str, Any]:
    from common.blackboard import EvidenceStore as BlackboardEvidenceStore
    from common.blackboard import FileBlackboard
    from common.evidence_store import FileEvidenceStore
    from coordinator.stores import MergedEvidenceStore
    from coordinator.synthesis import synthesize

    store = MergedEvidenceStore(BlackboardEvidenceStore(FileBlackboard(root=str(blackboard_dir))), FileEvidenceStore(evidence_dir))
    ids = sorted({p.name for p in evidence_dir.glob("*") if p.is_dir()} | {p.name for p in blackboard_dir.glob("*") if p.is_dir()})
    millis: list[float] = []
    rows: list[dict[str, Any]] = []
    unstable: list[str] = []
    skipped = 0
    replayed = 0
    fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for incident_id in ids:
        try:
            UUID(incident_id)
            documents = await store.read_all(incident_id)
        except Exception:  # noqa: BLE001 - a stray directory is not an incident
            skipped += 1
            continue
        if not documents:
            skipped += 1
            continue
        verdicts = set()
        for _ in range(repeats):
            start = time.perf_counter()
            try:
                report = synthesize(documents, now=fixed_now)
            except Exception:  # noqa: BLE001
                verdicts.add("error")
                break
            elapsed = (time.perf_counter() - start) * 1000
            millis.append(elapsed)
            rows.append({"part": "coordinator_replay", "group": "synthesize", "run": incident_id[:8], "value": elapsed, "unit": "ms"})
            verdicts.add(report.failure_type.value if hasattr(report, "failure_type") else str(getattr(report, "verdict", "")))
        replayed += 1
        if len(verdicts) != 1:
            unstable.append(incident_id)
    return {"incidents_replayed": replayed, "skipped": skipped, "millis": millis, "unstable": unstable, "rows": rows}


# ----------------------------------------------------------------------------------------------------------------------
# C. rule-path replay (measured)


def load_logs() -> list[dict[str, Any]]:
    """Every recorded build log; the four labelled ones carry a ground truth."""
    logs: list[dict[str, Any]] = []
    for directory in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
        log_path = directory / "build.log"
        if not log_path.exists():
            continue
        truth_path = directory / "ground_truth.json"
        truth = json.loads(truth_path.read_text(encoding="utf-8")) if truth_path.exists() else {}
        stage = "Checkout" if directory.name.startswith("git_checkout") else "Test"
        logs.append({"name": directory.name, "text": log_path.read_text(encoding="utf-8", errors="replace"), "expected": truth.get("expected_failure_type"), "stage": stage})
    for path in sorted(FIXTURES.glob("*.log")):
        logs.append({"name": path.stem, "text": path.read_text(encoding="utf-8", errors="replace"), "expected": None, "stage": "Test"})
    return logs


def rule_replay(logs: list[dict[str, Any]], repeats: int) -> dict[str, Any]:
    from agents.jenkins_agent.classifier import rule_based_classify
    from agents.jenkins_agent.parsing import parse_build

    millis: list[float] = []
    rows: list[dict[str, Any]] = []
    scored = right = 0
    misses: list[str] = []
    for log in logs:
        verdict = None
        for _ in range(repeats):
            start = time.perf_counter()
            parsed = parse_build(log["text"], failed_stage=log["stage"])
            hypotheses = rule_based_classify(parsed)
            elapsed = (time.perf_counter() - start) * 1000
            millis.append(elapsed)
            rows.append({"part": "rule_replay", "group": "parse+classify", "run": log["name"], "value": elapsed, "unit": "ms"})
            verdict = hypotheses[0].failure_type.value if hypotheses else "unknown"
        if log["expected"]:
            scored += 1
            if verdict == log["expected"]:
                right += 1
            else:
                misses.append(f'{log["name"]}: expected {log["expected"]}, got {verdict}')
    return {"logs": len(logs), "labelled": scored, "right": right, "misses": misses, "millis": millis, "rows": rows}


# ----------------------------------------------------------------------------------------------------------------------
# D. prompt variants (estimated)


def load_variants() -> list[dict[str, Any]]:
    import yaml

    return [yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted(PROMPTS.glob("*.yaml"))]


def tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def prompt_estimate(variant: dict[str, Any], log: dict[str, Any], *, turns: int, out_tokens: int, price_in: float, price_out: float) -> dict[str, Any]:
    """Tokens and dollars for one investigation under one prompt variant.

    Mirrors ``agents/jenkins_agent/agent.py``: the agent sends role + goal + backstory, then a task description naming the
    failed stage and error signature, and the model may call tools whose output (the build log tail) is added to the
    conversation. Each of ``turns`` model turns re-sends everything so far. The structure is the real one; the
    turn count, the output size and the prices are assumptions.
    """
    from agents.jenkins_agent.parsing import parse_build

    parsed = parse_build(log["text"], failed_stage=log["stage"])
    persona = " ".join(str(variant.get(key, "")) for key in ("role", "goal", "backstory"))
    task = (
        f"Investigate Jenkins failure for target-app #1.\nFailed Stage: {parsed.failed_stage or 'unknown'}\n"
        f"Error Signature: {parsed.error_signature or 'unknown'}\nFailing tests: {len(parsed.failing_tests)} total.\n"
        "Ground every claim in a cited evidence ID. If evidence is insufficient, say so with low confidence."
    )
    planning = 150 if variant.get("name") == "plan_execute" else 0  # a plan-then-execute prompt asks for an explicit plan first
    tool_output = min(tokens(log["text"]), 3000)  # the console tool truncates what it returns
    input_total = 0
    context = tokens(persona) + tokens(task) + planning
    for turn in range(turns):
        input_total += context
        context += out_tokens + (tool_output if turn == 0 else 0)
    output_total = out_tokens * turns
    dollars = input_total / 1e6 * price_in + output_total / 1e6 * price_out
    return {"input_tokens": input_total, "output_tokens": output_total, "usd": dollars}


def prompt_variants(logs: list[dict[str, Any]], *, turns: int, out_tokens: int, price_in: float, price_out: float) -> dict[str, Any]:
    variants = load_variants()
    rows: list[dict[str, Any]] = []
    by_variant: dict[str, dict[str, list[float]]] = {}
    for variant in variants:
        name = str(variant.get("name", "unnamed"))
        for log in logs:
            estimate = prompt_estimate(variant, log, turns=turns, out_tokens=out_tokens, price_in=price_in, price_out=price_out)
            bucket = by_variant.setdefault(name, {"usd": [], "input_tokens": []})
            bucket["usd"].append(estimate["usd"])
            bucket["input_tokens"].append(estimate["input_tokens"])
            rows.append({"part": "prompt_estimate", "group": name, "run": log["name"], "value": estimate["usd"], "unit": "usd"})
    return {"variants": [str(v.get("name")) for v in variants], "runs": len(rows), "by_variant": by_variant, "rows": rows}


# ----------------------------------------------------------------------------------------------------------------------
# E. sequential vs parallel dispatch (measured, from the stack)

DISPATCH_SQL = (
    "select incident_id || ',' || (payload->>'agent') || ',' || duration_ms from audit_events "
    "where event_type='agent_dispatch' and (payload->>'attempt')::int=0 and duration_ms is not null"
)


def dispatch_rows_from_stack() -> list[tuple[str, str, float]]:
    import subprocess

    command = ["docker", "compose", "exec", "-T", "postgres", "sh", "-c", f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "{DISPATCH_SQL}"']
    done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)
    if done.returncode != 0:
        raise RuntimeError(f"cannot read dispatch times from the stack: {done.stderr.strip()[:200]}")
    rows = []
    for line in done.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) == 3:
            rows.append((parts[0], parts[1], float(parts[2]) / 1000))
    return rows


def compare_dispatch(rows: list[tuple[str, str, float]], agents: int = 3) -> dict[str, Any]:
    """``rows`` are ``(incident, agent, seconds)``. Only incidents with all ``agents`` agents timed are compared; a
    sequential Coordinator would have waited for the sum, the parallel one for (about) the slowest."""
    per_incident: dict[str, dict[str, float]] = {}
    for incident, agent, seconds in rows:
        per_incident.setdefault(incident, {})[agent] = seconds
    complete = {i: t for i, t in per_incident.items() if len(t) == agents}
    sequential = [sum(t.values()) for t in complete.values()]
    parallel = [max(t.values()) for t in complete.values()]
    return {
        "incidents": len(complete),
        "skipped_incomplete": len(per_incident) - len(complete),
        "sequential": sequential,
        "parallel": parallel,
        "saved": [a - b for a, b in zip(sequential, parallel)],
        "slowest_agent": {agent: sum(1 for t in complete.values() if max(t, key=t.get) == agent) for agent in sorted({a for t in complete.values() for a in t})},
        "rows": [
            {"part": "dispatch_pattern", "group": kind, "run": incident[:8], "value": value, "unit": "s"}
            for incident, t in complete.items()
            for kind, value in (("sequential (sum)", sum(t.values())), ("parallel (max)", max(t.values())))
        ],
    }


# ----------------------------------------------------------------------------------------------------------------------
# report


def row(label: str, stats: dict[str, Any], digits: int = 2) -> str:
    if not stats.get("n"):
        return f"| {label} | 0 | | | | | |"
    f = lambda key: f"{stats[key]:.{digits}f}"  # noqa: E731
    return f"| {label} | {stats['n']} | {f('min')} | {f('median')} | {f('p95')} | {f('max')} | {f('mean')} |"


HEADER = "| Group | n | min | median | p95 | max | mean |\n|---|---|---|---|---|---|---|"


def dispatch_section(result: dict[str, Any]) -> list[str]:
    e = result.get("dispatch")
    if not e:
        return []
    return [
        "## E. Sequential vs parallel dispatch (measured)",
        "",
        f"The Coordinator runs its three agents in parallel. For {e['incidents']} real incidents with all three timed ({e['skipped_incomplete']} skipped as incomplete), "
        "a sequential Coordinator would have waited for the **sum** of the three; the parallel one waits for about the **slowest**.",
        "",
        HEADER,
        row("sequential (sum), s", e["sequential_stats"]),
        row("parallel (max), s", e["parallel_stats"]),
        row("time saved, s", e["saved_stats"]),
        "",
        f"Slowest agent per incident: {', '.join(f'{k} {v}' for k, v in e['slowest_agent'].items())}.",
        "",
        "![sequential vs parallel](performance_dispatch.svg)",
        "",
        "Caveat: the times were recorded while the agents ran in parallel, so a real sequential run could differ a little (no shared load),",
        "and the saving is bounded by the slowest agent: if one agent dominates, parallelism helps less than the sum suggests.",
        "",
    ]


def render_markdown(result: dict[str, Any]) -> str:
    a, b, c, d, cfg = result["recorded"], result["coordinator"], result["rules"], result["prompts"], result["assumptions"]
    out = [
        "# End-to-end performance evaluation",
        "",
        f"_Generated {result['generated_at']} by `scripts/evaluate_performance.py`. Replayed from recorded data: **no LLM was called**._",
        "",
        "Every number is labelled **measured** (timed on real runs or by running the real code on recorded inputs) or **estimated**",
        "(computed from prompt sizes and an assumed price table). Do not read an estimate as a measurement.",
        "",
        "## A. Recorded runs on the real stack (measured)",
        "",
        f"{a['incidents']} incidents in the audit trail; {a['unfinished_runs_excluded']} runs with no end event (a crashed process) are excluded, not counted as fast.",
        "",
        "Run time in **seconds**, from the agent starting to the agent finishing:",
        "",
        HEADER,
    ]
    for agent, stats in a["agent_stats"].items():
        out.append(row(agent, stats))
    if "jenkins_agent" not in a["agent_stats"]:
        out += ["", "> The Jenkins agent writes no audit events (the audit check is told to expect that), so its run time is **not** in this table."]
    out += ["", "Incident opened to first Coordinator report, seconds:", "", HEADER, row("incident -> report", a["end_to_end_stats"]), ""]
    out += ["Tool-call latency, **milliseconds**:", "", HEADER]
    for agent, stats in a["tool_stats"].items():
        out.append(row(agent, stats))
    out += [
        "",
        "![agent run time](performance_agent_runtime.svg)",
        "",
        "## B. Coordinator replay (measured)",
        "",
        f"The recorded evidence of {b['incidents_replayed']} incidents ({b['skipped']} directories skipped) was fused {result['repeats']} times each by the real `synthesize`.",
        "",
        HEADER,
        row("synthesize (ms)", b["stats"], 3),
        "",
        f"Verdict identical on every repeat for every incident: **{'yes' if not b['unstable'] else 'NO - ' + str(len(b['unstable'])) + ' unstable'}**.",
        "",
        "## C. Rule path replay (measured)",
        "",
        f"{c['logs']} recorded build logs through the real Jenkins-agent parser and classifier, {result['repeats']} times each.",
        "",
        HEADER,
        row("parse + classify (ms)", c["stats"], 3),
        "",
        f"Accuracy on the {c['labelled']} labelled logs: **{c['right']}/{c['labelled']}**." + ("" if not c["misses"] else " Misses: " + "; ".join(c["misses"]) + "."),
        "",
        "## D. Prompt variants (ESTIMATED, no LLM called)",
        "",
        f"{d['runs']} estimated runs = {len(d['variants'])} prompt variants x {d['runs'] // max(len(d['variants']), 1)} build logs.",
        f"Assumptions (change them with flags): {cfg['turns']} model turns per investigation, {cfg['out_tokens']} output tokens per turn, "
        f"{cfg['price_in']} USD per 1M input tokens, {cfg['price_out']} USD per 1M output tokens, {CHARS_PER_TOKEN} characters per token.",
        "",
        "Estimated cost of one investigation, **USD**:",
        "",
        HEADER,
    ]
    for name, stats in d["cost_stats"].items():
        out.append(row(name, stats, 5))
    out += [
        "",
        "![estimated cost per variant](performance_prompt_cost.svg)",
        "",
        *dispatch_section(result),
        "## What this does and does not show",
        "",
        "- Parts A-C are timings of real code on real or recorded inputs. They show the pipeline outside the LLM is fast (milliseconds); the",
        "  seconds in part A are dominated by the agents' tool calls and, for the Jenkins agent, by the LLM and Jenkins itself.",
        "- Part D estimates cost from the size of what would be sent. **LLM latency and the accuracy of each prompt variant are not measured**:",
        "  that needs a live run per variant (`PROMPT_VARIANT=<name>` with `scripts/evaluate_jenkins_agent.py`), which this script was written to avoid.",
        "- Nothing writes `llm_call` events yet, so the audit trail holds no measured token counts to replay; once it does, replace part D with them.",
        "- The prompt variants share almost all of their text, so the estimated cost differs mostly across build logs, not across variants. That is",
        "  itself the finding: the persona is cheap, the log is what costs money.",
        "",
    ]
    return "\n".join(out)


def build(args: argparse.Namespace) -> dict[str, Any]:
    recorded = recorded_runs(args.audit_dir)
    coordinator = asyncio.run(coordinator_replay(args.evidence_dir, args.blackboard_dir, args.repeats))
    logs = load_logs()
    rules = rule_replay(logs, args.repeats)
    dispatch = None
    if args.stack:
        dispatch = compare_dispatch(dispatch_rows_from_stack())
        dispatch["sequential_stats"], dispatch["parallel_stats"], dispatch["saved_stats"] = (describe(dispatch[k]) for k in ("sequential", "parallel", "saved"))
    prompts = prompt_variants(logs, turns=args.turns, out_tokens=args.out_tokens, price_in=args.price_in, price_out=args.price_out)

    recorded["agent_stats"] = {agent: describe(values) for agent, values in sorted(recorded["agent_seconds"].items())}
    recorded["end_to_end_stats"] = describe(recorded["end_to_end_seconds"])
    recorded["tool_stats"] = {agent: describe(values) for agent, values in sorted(recorded["tool_ms"].items())}
    coordinator["stats"] = describe(coordinator["millis"])
    rules["stats"] = describe(rules["millis"])
    prompts["cost_stats"] = {name: describe(values["usd"]) for name, values in prompts["by_variant"].items()}
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "repeats": args.repeats,
        "assumptions": {"turns": args.turns, "out_tokens": args.out_tokens, "price_in": args.price_in, "price_out": args.price_out},
        "recorded": recorded,
        "coordinator": coordinator,
        "rules": rules,
        "prompts": prompts,
        "dispatch": dispatch,
    }


def write_outputs(result: dict[str, Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    def put(name: str, text: str) -> None:
        path = out_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)

    all_rows = result["recorded"]["rows"] + result["coordinator"]["rows"] + result["rules"]["rows"] + result["prompts"]["rows"]
    if result.get("dispatch"):
        all_rows += result["dispatch"]["rows"]
    csv_path = out_dir / "performance_runs.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["part", "group", "run", "value", "unit"])
        writer.writeheader()
        writer.writerows(all_rows)
    written.append(csv_path)

    slim = json.loads(json.dumps(result, default=str))
    for part in ("recorded", "coordinator", "rules", "prompts", "dispatch"):
        for key in ("rows", "millis", "agent_seconds", "tool_ms", "end_to_end_seconds", "by_variant", "sequential", "parallel", "saved"):
            (slim.get(part) or {}).pop(key, None)
    put("performance_eval.json", json.dumps(slim, indent=2))
    put("performance_eval.md", render_markdown(result))
    put("performance_agent_runtime.svg", box_svg(result["recorded"]["agent_seconds"] | {"incident->report": result["recorded"]["end_to_end_seconds"]}, title="Recorded run time per agent (measured)", unit="seconds", log_scale=True))
    put("performance_prompt_cost.svg", box_svg({n: v["usd"] for n, v in result["prompts"]["by_variant"].items()}, title="Estimated cost per investigation by prompt variant (estimate)", unit="USD"))
    if result.get("dispatch"):
        d = result["dispatch"]
        put("performance_dispatch.svg", box_svg({"sequential (sum)": d["sequential"], "parallel (max)": d["parallel"]}, title="Dispatch time per incident: sequential vs parallel (measured)", unit="seconds", log_scale=True))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audit-dir", type=Path, default=Path(os.getenv("AUDIT_DIR", ROOT / "data" / "audit")))
    parser.add_argument("--evidence-dir", type=Path, default=Path(os.getenv("EVIDENCE_DIR", ROOT / "data" / "evidence")))
    parser.add_argument("--blackboard-dir", type=Path, default=Path(os.getenv("BLACKBOARD_DIR", ROOT / "data" / "blackboard")))
    parser.add_argument("--out", type=Path, default=ROOT / "results")
    parser.add_argument("--stack", action="store_true", help="also compare sequential vs parallel dispatch, reading dispatch times from the running Docker stack")
    parser.add_argument("--repeats", type=int, default=5, help="replays per input for parts B and C")
    parser.add_argument("--turns", type=int, default=3, help="assumed model turns per investigation (part D)")
    parser.add_argument("--out-tokens", type=int, default=400, help="assumed output tokens per turn (part D)")
    parser.add_argument("--price-in", type=float, default=0.30, help="ASSUMED USD per 1M input tokens; check your model's price")
    parser.add_argument("--price-out", type=float, default=2.50, help="ASSUMED USD per 1M output tokens; check your model's price")
    args = parser.parse_args(argv)

    result = build(args)
    for path in write_outputs(result, args.out):
        print(f"wrote {path}")
    a, b, c, d = result["recorded"], result["coordinator"], result["rules"], result["prompts"]
    print(
        f"recorded: {a['incidents']} incidents, {sum(len(v) for v in a['agent_seconds'].values())} agent runs | "
        f"coordinator replay: {b['incidents_replayed']} incidents, {len(b['millis'])} runs, unstable={len(b['unstable'])} | "
        f"rules: {c['logs']} logs, {c['right']}/{c['labelled']} labelled right | prompts: {d['runs']} estimated runs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
