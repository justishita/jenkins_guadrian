#!/usr/bin/env python3
"""Quantitative evaluation of the Metrics agent against a naive threshold baseline. Owner: P2.

Three parts, all offline (no Prometheus, RabbitMQ or LLM needed):

  1. Scenario benchmark - every `scenarios/*.yaml` with `agent: metrics_agent`, driven through
     the real investigator. Reports accuracy, per-class precision/recall, a confusion matrix,
     and the false-positive rate on cases where the correct answer is "metrics do not explain it".
  2. Severity sweep - fault magnitude and noise are varied programmatically to show where the
     agent and the baseline start detecting (and where they raise false alarms).
  3. Live runs (optional, `--live labels.json`) - evidence files written by real
     `scripts/inject_faults.py` runs, scored against the fault you injected.

Two comparison systems (neither is part of the product):
  * threshold baseline - fixed alerting thresholds applied to the max/min of the incident
    window, no baseline period, no persistence or correlation logic. When several fire, the
    largest ratio to its own threshold wins. Thresholds are CLI-tunable.
  * LLM baseline (`--llm`) - Gemini Flash via common.llm_client is given the SAME Prometheus
    series the agent queried (baseline period and incident window) and asked for a failure type
    in the shared taxonomy. Temperature 0, one call per case, answers cached in `--llm-cache`
    so a re-run costs nothing and gives the same numbers. Calls that fail are excluded from the
    LLM's scores and counted, never silently treated as "unknown".

Usage (from the repo root):
  python -m scripts.score_metrics_agent                 # print the report
  python -m scripts.score_metrics_agent --json out.json # also write machine-readable results
  python -m scripts.score_metrics_agent --live labels.json [--evidence-dir data/evidence]
  python -m scripts.score_metrics_agent --llm           # adds the Gemini baseline (needs GEMINI_API_KEY)

labels.json maps an incident id (or unique prefix) to the injected fault:
  {"0760fc59": "cpu", "55115f7b": "slow", "b79d7040": "healthy"}
  faults: slow | cpu | leak | down | healthy
"""

import argparse
import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from agents.metrics_agent.investigator import InvestigationConfig
from agents.metrics_agent.models import QueryResult
from agents.metrics_agent.prometheus_tool import PrometheusError
from agents.metrics_agent.queries import AVAILABILITY, CPU_RATE, LATENCY_P95, MEMORY_RSS
from agents.metrics_agent.window import build_window
from common.llm_client import LLMClient
from common.models import FailureTaxonomy
from tests.metrics_agent.helpers import FAILURE_TIME, MIB, NOW
from tests.metrics_agent.scenario_runner import (
    ScenarioTool,
    investigate,
    load_metrics_scenarios,
)

logger = logging.getLogger("score_metrics_agent")

UNKNOWN = FailureTaxonomy.UNKNOWN.value
TIMEOUT = FailureTaxonomy.TIMEOUT.value
RESOURCE = FailureTaxonomy.RESOURCE_EXHAUSTION.value
NETWORK = FailureTaxonomy.INFRA_NETWORK_FAILURE.value

FAULT_TO_TYPE = {"slow": TIMEOUT, "cpu": RESOURCE, "leak": RESOURCE, "down": NETWORK, "healthy": UNKNOWN}


# --- baseline ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    latency_p95_s: float = 1.0
    cpu_cores: float = 0.8
    memory_bytes: float = 512 * MIB


def baseline_predict(scenario: dict[str, Any], t: Thresholds) -> str:
    """Fixed-threshold alerting over the incident window: no baseline period, no persistence."""
    tool = ScenarioTool(scenario)
    cfg = InvestigationConfig()
    window = build_window(FAILURE_TIME, cfg.lookback, cfg.tail, cfg.baseline, now=NOW)

    def incident_values(promql: str) -> list[float]:
        try:
            result: QueryResult = tool.query_range(promql, window.baseline_start, window.end, cfg.step)
        except PrometheusError:
            return []
        return [s.value for series in result.series for s in series.samples if s.timestamp >= window.start]

    scores: dict[str, float] = {}

    def fire(failure_type: str, ratio: float) -> None:
        if ratio >= 1.0:
            scores[failure_type] = max(scores.get(failure_type, 0.0), ratio)

    up = incident_values(AVAILABILITY.promql)
    if up and min(up) == 0:
        fire(NETWORK, 1e9)  # an outage outranks everything, as an on-call engineer would treat it
    latency = incident_values(LATENCY_P95.promql)
    if latency:
        fire(TIMEOUT, max(latency) / t.latency_p95_s)
    cpu = incident_values(CPU_RATE.promql)
    if cpu:
        fire(RESOURCE, max(cpu) / t.cpu_cores)
    memory = incident_values(MEMORY_RSS.promql)
    if memory:
        fire(RESOURCE, max(memory) / t.memory_bytes)
    return max(scores, key=lambda k: scores[k]) if scores else UNKNOWN


def agent_predict(scenario: dict[str, Any]) -> tuple[str, float, str]:
    with tempfile.TemporaryDirectory() as tmp:
        evidence, _, _ = investigate(scenario, Path(tmp))
    return evidence.failure_type.value, evidence.confidence, evidence.status


# --- LLM baseline -----------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an SRE investigating a failed CI/CD pipeline run. You are given Prometheus metrics "
    "for the application under test: a baseline period (normal behaviour) and the incident "
    "window around the failure. Decide which failure type, if any, the METRICS support. "
    "Answer 'unknown' if the metrics do not show a problem or the data is missing, unusable or "
    "too thin to conclude. Do not guess a cause the data does not show."
)

TAXONOMY_HELP = "\n".join(f"- {t.value}" for t in FailureTaxonomy)


class LLMVerdict(BaseModel):
    failure_type: FailureTaxonomy
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


def _fmt(name: str, value: float) -> str:
    return f"{value / MIB:.1f}MiB" if name == "memory_rss" else f"{value:.4g}"


def llm_prompt(scenario: dict[str, Any]) -> str:
    """Render the same query results the agent saw, as text. Nothing the agent does not see."""
    tool = ScenarioTool(scenario)
    cfg = InvestigationConfig()
    window = build_window(FAILURE_TIME, cfg.lookback, cfg.tail, cfg.baseline, now=NOW)
    stage = (scenario.get("incident") or {}).get("failed_stage") or "unknown"
    lines = [
        f"Failed pipeline stage: {stage}",
        f"Failure time: {FAILURE_TIME.isoformat()}",
        f"Baseline period: {window.baseline_start.isoformat()} to {window.start.isoformat()}",
        f"Incident window: {window.start.isoformat()} to {window.end.isoformat()} (samples every {cfg.step})",
        "",
    ]
    for spec in (AVAILABILITY, LATENCY_P95, CPU_RATE, MEMORY_RSS):
        lines.append(f"Metric {spec.name} ({spec.unit}), PromQL: {spec.promql}")
        try:
            result = tool.query_range(spec.promql, window.baseline_start, window.end, cfg.step)
        except PrometheusError as exc:
            lines += [f"  query failed: {exc}", ""]
            continue
        samples = [s for series in result.series for s in series.samples]
        if not samples:
            lines += ["  no data returned", ""]
            continue
        base = sorted(s.value for s in samples if s.timestamp < window.start)
        incident = [s.value for s in samples if s.timestamp >= window.start]
        if base:
            lines.append(
                f"  baseline: median {_fmt(spec.name, base[len(base) // 2])}, "
                f"min {_fmt(spec.name, base[0])}, max {_fmt(spec.name, base[-1])} ({len(base)} samples)"
            )
        else:
            lines.append("  baseline: no samples")
        lines.append(f"  incident window values: {[_fmt(spec.name, v) for v in incident]}")
        lines.append("")
    lines += ["Allowed failure types:", TAXONOMY_HELP]
    return "\n".join(lines)


class LLMRunner:
    """Asks the LLM for a verdict per scenario, with an on-disk cache keyed by prompt hash."""

    def __init__(self, client: Any, cache_path: Path | None = None) -> None:
        self._client = client
        self._cache_path = cache_path
        self._cache: dict[str, dict[str, Any]] = {}
        if cache_path and cache_path.exists():
            self._cache = json.loads(cache_path.read_text(encoding="utf-8"))
        self._loop = asyncio.new_event_loop()  # one loop for the client's rate limiter
        self.errors = 0

    def predict(self, scenario: dict[str, Any]) -> tuple[str, float] | None:
        prompt = llm_prompt(scenario)
        # The model is part of the key: answers from different models must never be mixed.
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        key = hashlib.sha256((model + SYSTEM_PROMPT + prompt).encode()).hexdigest()
        if key not in self._cache:
            try:
                raw = self._loop.run_until_complete(
                    self._client.chat(
                        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
                        response_schema=LLMVerdict,
                    )
                )
                verdict = raw if isinstance(raw, LLMVerdict) else LLMVerdict.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 - boundary: one bad call must not abort the whole evaluation
                logger.warning("LLM call failed (%s: %s)", type(exc).__name__, exc)
                self.errors += 1
                return None
            self._cache[key] = {
                "model": model,
                "failure_type": verdict.failure_type.value,
                "confidence": verdict.confidence,
                "reasoning": verdict.reasoning,
            }
            if self._cache_path:
                self._cache_path.write_text(json.dumps(self._cache, indent=2), encoding="utf-8")
        entry = self._cache[key]
        return entry["failure_type"], float(entry["confidence"])


# --- metrics ----------------------------------------------------------------------------


def confusion(pairs: list[tuple[str, str]]) -> dict[str, Counter[str]]:
    matrix: dict[str, Counter[str]] = defaultdict(Counter)
    for truth, predicted in pairs:
        matrix[truth][predicted] += 1
    return matrix


def per_class(pairs: list[tuple[str, str]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for label in sorted({t for t, _ in pairs} | {p for _, p in pairs}):
        tp = sum(t == label and p == label for t, p in pairs)
        fp = sum(t != label and p == label for t, p in pairs)
        fn = sum(t == label and p != label for t, p in pairs)
        precision = tp / (tp + fp) if tp + fp else float("nan")
        recall = tp / (tp + fn) if tp + fn else float("nan")
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else float("nan")
        out[label] = {"precision": precision, "recall": recall, "f1": f1, "support": tp + fn}
    return out


def summarize(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    correct = sum(t == p for t, p in pairs)
    negatives = [(t, p) for t, p in pairs if t == UNKNOWN]
    positives = [(t, p) for t, p in pairs if t != UNKNOWN]
    return {
        "n": len(pairs),
        "accuracy": correct / len(pairs) if pairs else float("nan"),
        # Restraint: of the cases metrics do NOT explain, how often did the system name a cause anyway?
        "false_positive_rate": sum(p != UNKNOWN for _, p in negatives) / len(negatives) if negatives else float("nan"),
        "n_negatives": len(negatives),
        # Of the real faults, how many got the exactly right type? (A wrong type is a miss.)
        "fault_recall": sum(t == p for t, p in positives) / len(positives) if positives else float("nan"),
        "n_faults": len(positives),
        "per_class": per_class(pairs),
        "confusion": {t: dict(c) for t, c in confusion(pairs).items()},
    }


# --- 1. scenario benchmark --------------------------------------------------------------


def run_scenarios(thresholds: Thresholds, llm: LLMRunner | None = None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for scenario in load_metrics_scenarios():
        truth = scenario["expected"]["failure_type"]
        predicted, confidence, status = agent_predict(scenario)
        guess = llm.predict(scenario) if llm else None
        rows.append(
            {
                "llm": guess[0] if guess else None,
                "llm_confidence": guess[1] if guess else None,
                "id": scenario["id"],
                "file": scenario["_file"],
                "truth": truth,
                "agent": predicted,
                "agent_confidence": confidence,
                "agent_status": status,
                "baseline": baseline_predict(scenario, thresholds),
            }
        )
    return {
        "rows": rows,
        "agent": summarize([(r["truth"], r["agent"]) for r in rows]),
        "baseline": summarize([(r["truth"], r["baseline"]) for r in rows]),
        "llm": summarize([(r["truth"], r["llm"]) for r in rows if r["llm"]]) if llm else None,
        "llm_errors": llm.errors if llm else 0,
        "confidence_correct": _mean([r["agent_confidence"] for r in rows if r["truth"] == r["agent"]]),
        "confidence_wrong": _mean([r["agent_confidence"] for r in rows if r["truth"] != r["agent"]]),
    }


# --- 2. severity sweep ------------------------------------------------------------------

LATENCY_LEVELS = [0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0]  # seconds (healthy ~0.007)
CPU_LEVELS = [0.01, 0.03, 0.06, 0.1, 0.2, 0.5, 0.9]  # cores (healthy ~0.002)
LEAK_GROWTH_MIB = [1, 3, 5, 10, 20, 40, 80]  # MiB over 4 minutes (healthy 80 MiB flat)
NOISE_MULTIPLIERS = [0.0, 0.25, 0.5, 1.0, 2.0]


def _scenario(metrics: dict[str, Any]) -> dict[str, Any]:
    return {"incident": {"branch": "main", "failed_stage": "Test"}, "metrics": copy.deepcopy(metrics)}


def sweep_cases() -> list[tuple[str, str, float, dict[str, Any], str]]:
    """(fault kind, label, magnitude, scenario, expected failure type)."""
    cases: list[tuple[str, str, float, dict[str, Any], str]] = []
    for level in LATENCY_LEVELS:
        cases.append(("slow", f"latency step to {level:g}s", level, _scenario({"latency_p95": {"fault": {"shape": "step", "at": -120, "level": level}}}), TIMEOUT))
    for level in CPU_LEVELS:
        cases.append(("cpu", f"cpu step to {level:g} cores", level, _scenario({"cpu_rate": {"fault": {"shape": "step", "at": -120, "level": level}}}), RESOURCE))
    for growth in LEAK_GROWTH_MIB:
        level = (80 + growth) * MIB
        cases.append(("leak", f"memory +{growth} MiB", growth, _scenario({"memory_rss": {"fault": {"shape": "ramp", "at": -240, "until": 0, "level": level}}}), RESOURCE))
    for mult in NOISE_MULTIPLIERS:
        noise = {
            "latency_p95": {"noise": 0.002 * mult},
            "cpu_rate": {"baseline": 0.3, "noise": 0.05 * mult},
            "memory_rss": {"noise": 524288 * mult},
        }
        cases.append(("healthy", f"healthy, noise x{mult:g}", mult, _scenario(noise), UNKNOWN))
    return cases


def run_sweep(thresholds: Thresholds, llm: LLMRunner | None = None) -> list[dict[str, Any]]:
    rows = []
    for kind, label, magnitude, scenario, truth in sweep_cases():
        predicted, confidence, _ = agent_predict(scenario)
        guess = llm.predict(scenario) if llm else None
        rows.append(
            {
                "llm": guess[0] if guess else None,
                "llm_confidence": guess[1] if guess else None,
                "kind": kind,
                "label": label,
                "magnitude": magnitude,
                "truth": truth,
                "agent": predicted,
                "agent_confidence": confidence,
                "baseline": baseline_predict(scenario, thresholds),
            }
        )
    return rows


# --- 3. live evidence -------------------------------------------------------------------


def run_live(labels_path: Path, evidence_dir: Path) -> dict[str, Any]:
    labels: dict[str, str] = json.loads(labels_path.read_text(encoding="utf-8"))
    dirs = [d for d in evidence_dir.iterdir() if d.is_dir()]
    rows = []
    for prefix, fault in labels.items():
        if fault not in FAULT_TO_TYPE:
            raise SystemExit(f"unknown fault {fault!r} for {prefix}; use one of {sorted(FAULT_TO_TYPE)}")
        matches = [d for d in dirs if d.name.startswith(prefix)]
        if len(matches) != 1:
            raise SystemExit(f"label {prefix!r} matches {len(matches)} incidents in {evidence_dir}")
        document = json.loads((matches[0] / "metrics_agent.json").read_text(encoding="utf-8"))
        rows.append(
            {
                "incident": matches[0].name,
                "fault": fault,
                "truth": FAULT_TO_TYPE[fault],
                "agent": document["failure_type"],
                "agent_confidence": document["confidence"],
                "status": document["status"],
            }
        )
    return {"rows": rows, "agent": summarize([(r["truth"], r["agent"]) for r in rows])}


# --- reporting --------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def _pct(value: float) -> str:
    return "n/a" if math.isnan(value) else f"{value:.0%}"


def _print_summary(title: str, s: dict[str, Any]) -> None:
    print(f"\n{title}")
    print(f"  accuracy {_pct(s['accuracy'])} (n={s['n']})   fault recall {_pct(s['fault_recall'])} (n={s['n_faults']})"
          f"   false-positive rate {_pct(s['false_positive_rate'])} (n={s['n_negatives']})")
    print(f"  {'class':<26}{'precision':>10}{'recall':>8}{'f1':>6}{'support':>9}")
    for label, m in s["per_class"].items():
        print(f"  {label:<26}{_pct(m['precision']):>10}{_pct(m['recall']):>8}{_pct(m['f1']):>6}{m['support']:>9}")


def _print_confusion(title: str, s: dict[str, Any]) -> None:
    labels = sorted({*s["confusion"], *(p for row in s["confusion"].values() for p in row)})
    print(f"\n{title} (rows = truth, columns = prediction)")
    print("  " + " " * 26 + "".join(f"{label[:12]:>14}" for label in labels))
    for truth in sorted(s["confusion"]):
        print(f"  {truth:<26}" + "".join(f"{s['confusion'][truth].get(p, 0):>14}" for p in labels))


def _mark(row: dict[str, Any], name: str) -> str:
    if row.get(name) is None:
        return "ERROR"
    if row["truth"] == row[name]:
        return "ok"
    return "MISS" if row["truth"] != UNKNOWN else "FALSE+"


def print_report(bench: dict[str, Any], sweep: list[dict[str, Any]], live: dict[str, Any] | None, t: Thresholds) -> None:
    print("=" * 78)
    print("Metrics agent - quantitative evaluation")
    print("=" * 78)
    print(f"Baseline thresholds: latency_p95 > {t.latency_p95_s:g}s, cpu > {t.cpu_cores:g} cores, "
          f"memory > {t.memory_bytes / MIB:g} MiB, any availability drop")

    has_llm = bench.get("llm") is not None
    print("\n--- 1. Scenario benchmark ---")
    header = f"  {'scenario':<44}{'truth':<22}{'agent':<22}{'conf':>5}  {'threshold':<22}"
    print(header + (f"{'llm (gemini)':<22}" if has_llm else ""))
    for r in bench["rows"]:
        flags = [n for n in ("agent", "baseline", "llm") if (n != "llm" or has_llm) and r.get(n) != r["truth"]]
        llm_col = f"{(r['llm'] or 'ERROR'):<22}" if has_llm else ""
        print(f"  {r['file']:<44}{r['truth']:<22}{r['agent']:<22}{r['agent_confidence']:>5.2f}  "
              f"{r['baseline']:<22}{llm_col}" + (f"  <- wrong: {', '.join(flags)}" if flags else ""))
    _print_summary("Agent", bench["agent"])
    _print_summary("Threshold baseline", bench["baseline"])
    if has_llm:
        _print_summary(f"LLM baseline (Gemini; {bench['llm_errors']} failed call(s) excluded)", bench["llm"])
    _print_confusion("Agent confusion matrix", bench["agent"])
    _print_confusion("Threshold baseline confusion matrix", bench["baseline"])
    if has_llm:
        _print_confusion("LLM baseline confusion matrix", bench["llm"])
    print(f"\n  Mean agent confidence when correct {bench['confidence_correct']:.2f}, "
          f"when wrong {'n/a' if bench['confidence_wrong'] != bench['confidence_wrong'] else format(bench['confidence_wrong'], '.2f')}")
    if has_llm:
        llm_rows = [r for r in bench["rows"] if r["llm"]]
        right = [r["llm_confidence"] for r in llm_rows if r["llm"] == r["truth"]]
        wrong = [r["llm_confidence"] for r in llm_rows if r["llm"] != r["truth"]]
        print(f"  Mean LLM confidence when correct {_mean(right):.2f}, "
              f"when wrong {'n/a' if not wrong else format(_mean(wrong), '.2f')}")

    systems = ["agent", "baseline"] + (["llm"] if any(r.get("llm") for r in sweep) else [])
    print("\n--- 2. Severity sweep (detected with the correct failure type?) ---")
    print(f"  {'case':<28}" + "".join(f"{n:<10}" for n in systems))
    for r in sweep:
        print(f"  {r['label']:<28}" + "".join(f"{_mark(r, n):<10}" for n in systems))
    faults = [r for r in sweep if r["truth"] != UNKNOWN]
    healthy = [r for r in sweep if r["truth"] == UNKNOWN]
    for name in systems:
        f_ok = [r for r in faults if r.get(name) is not None]
        h_ok = [r for r in healthy if r.get(name) is not None]
        hits = sum(r["truth"] == r[name] for r in f_ok)
        print(f"  {name:<9} fault detection {_pct(hits / len(f_ok)) if f_ok else 'n/a'} ({hits}/{len(f_ok)}), "
              f"false alarms on healthy {sum(r[name] != UNKNOWN for r in h_ok)}/{len(h_ok)}")

    if live:
        print("\n--- 3. Live runs ---")
        for r in live["rows"]:
            flag = "" if r["truth"] == r["agent"] else "  <- wrong"
            print(f"  {r['incident'][:8]}  injected {r['fault']:<8} -> {r['agent']:<22}{r['agent_confidence']:>5.2f}{flag}")
        _print_summary("Agent (live)", live["agent"])
        _print_confusion("Agent confusion matrix (live)", live["agent"])

    print("\nCaveats: scenarios and sweep are synthetic with deterministic noise; the baseline thresholds are "
          "illustrative; the LLM is one run at temperature 0 on a text rendering of the same series; "
          "live runs are as labelled by you.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", type=Path, help="also write results to this JSON file")
    parser.add_argument("--live", type=Path, help="labels.json for stored live evidence")
    parser.add_argument("--evidence-dir", type=Path, default=Path("data/evidence"))
    parser.add_argument("--llm", action="store_true", help="add a Gemini baseline (needs GEMINI_API_KEY, ~40 calls)")
    parser.add_argument("--llm-cache", type=Path, default=Path("data/llm_baseline_cache.json"))
    parser.add_argument("--llm-scope", choices=["scenarios", "all"], default="all",
                        help="'scenarios' sends only the 14 scenarios to Gemini (fits a 20-requests/day free tier)")
    parser.add_argument("--latency-threshold", type=float, default=Thresholds.latency_p95_s)
    parser.add_argument("--cpu-threshold", type=float, default=Thresholds.cpu_cores)
    parser.add_argument("--memory-threshold-mib", type=float, default=Thresholds.memory_bytes / MIB)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    # The investigator logs one line per incident; keep the report readable.
    logging.getLogger("agents").setLevel(logging.ERROR)

    thresholds = Thresholds(args.latency_threshold, args.cpu_threshold, args.memory_threshold_mib * MIB)
    llm = None
    if args.llm:
        load_dotenv()
        try:
            llm = LLMRunner(LLMClient(), args.llm_cache)
        except ValueError as exc:
            raise SystemExit(f"cannot start the LLM baseline: {exc}") from exc
    bench = run_scenarios(thresholds, llm)
    sweep = run_sweep(thresholds, llm if args.llm_scope == "all" else None)
    live = run_live(args.live, args.evidence_dir) if args.live else None
    print_report(bench, sweep, live, thresholds)

    if args.json:
        args.json.write_text(
            json.dumps({"thresholds": thresholds.__dict__, "benchmark": bench, "sweep": sweep, "live": live}, indent=2),
            encoding="utf-8",
        )
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    sys.exit(main())
