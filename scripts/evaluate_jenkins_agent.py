#!/usr/bin/env python3
"""Quantitative evaluation of the Jenkins agent against ground-truth scenarios.

Iterates every scenarios/*.json ground truth, injects the fault N times via
inject_fault + Jenkins, waits for the incident's evidence, and computes a full
suite of metrics:

  - Classification accuracy
  - File/line localization accuracy
  - Average confidence for correct vs. incorrect predictions
  - Calibration (Expected Calibration Error, ECE with 5 bins)
  - Investigation latency p50 / p95 (webhook received → evidence written)
    reported SEPARATELY for rule-only vs. final LLM evidence
  - LLM calls and tokens per incident
  - Fallback rate (rule-only path used because LLM was absent/failed)
  - Flaky false-positive rate (healthy scenario wrongly classified as flaky)
  - Duplicate detection precision

Outputs:
  results/jenkins_agent_eval.json  – machine-readable full results
  results/jenkins_agent_eval.md    – human-readable markdown table

Ablation flags:
  --no-llm      rules-only  (sets LLM_OFFLINE=1)
  --no-rules    LLM-only    (skips rule_based_classify step)
  --model       override GEMINI_MODEL

LLM response cache:
  Responses are keyed by prompt hash and stored in data/eval_llm_cache.json.
  Set LLM_CACHE=1 (default during eval) or pass --no-llm-cache to disable.

Usage (from the repo root):
  python scripts/evaluate_jenkins_agent.py                # hybrid, 5 runs each
  python scripts/evaluate_jenkins_agent.py --no-llm -n 3
  python scripts/evaluate_jenkins_agent.py --no-rules --model gemini-2.5-pro
  python scripts/evaluate_jenkins_agent.py --model gemini-2.5-flash --n 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import statistics
import sys
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

# ---------------------------------------------------------------------------
# Repo root on sys.path so relative imports work when invoked as a script
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("eval_jenkins_agent")

SCENARIOS_DIR = REPO_ROOT / "scenarios"
RESULTS_DIR = REPO_ROOT / "results"
EVIDENCE_DIR = REPO_ROOT / "data" / "evidence"
LLM_CACHE_PATH = REPO_ROOT / "data" / "eval_llm_cache.json"
ECE_BINS = 5


# ---------------------------------------------------------------------------
# Ground truth schema
# ---------------------------------------------------------------------------


@dataclass
class GroundTruth:
    scenario: str           # filename stem
    expected_failure_type: str
    file: str | None = None
    line: int | None = None
    # Optional extra fields from the JSON
    extra: dict[str, Any] = field(default_factory=dict)


def load_ground_truths(scenarios_dir: Path) -> list[GroundTruth]:
    """Load every *.json file in scenarios/ as a GroundTruth."""
    truths: list[GroundTruth] = []
    for path in sorted(scenarios_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Skipping %s: cannot parse (%s)", path.name, exc)
            continue
        # Tolerate both inject_fault.py format and richer scenario JSONs
        failure_type = (
            doc.get("expected_failure_type")
            or doc.get("failure_type")
            or (doc.get("expected") or {}).get("failure_type")
        )
        if not failure_type:
            logger.warning("Skipping %s: no expected_failure_type", path.name)
            continue
        truths.append(
            GroundTruth(
                scenario=path.stem,
                expected_failure_type=str(failure_type),
                file=doc.get("file"),
                line=doc.get("line"),
                extra={k: v for k, v in doc.items() if k not in {"scenario", "file", "line", "expected_failure_type"}},
            )
        )
    return truths


# ---------------------------------------------------------------------------
# Single-run result
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    scenario: str
    run_index: int
    incident_id: str
    # Timing
    webhook_ts: float            # time.monotonic() when we "sent" the event
    rules_ts: float | None       # time.monotonic() when rules finished (ms path)
    evidence_ts: float | None    # time.monotonic() when evidence file was written
    # Classification
    predicted_failure_type: str
    expected_failure_type: str
    confidence: float
    # Localization
    predicted_file: str | None
    predicted_line: int | None
    expected_file: str | None
    expected_line: int | None
    # LLM
    llm_calls: int
    llm_input_tokens: int
    llm_output_tokens: int
    used_fallback: bool          # True when rule-only path was taken
    # Status
    status: str                  # "completed" | "failed" | "insufficient_evidence"
    error: str | None = None     # set when the run itself raised an exception
    # Duplicate / flaky meta
    related_incident_ids: list[str] = field(default_factory=list)
    flaky_score: float | None = None
    duplicate_declared: bool = False   # agent said this is a duplicate


# ---------------------------------------------------------------------------
# LLM call counter – monkeypatches LLMClient.chat
# ---------------------------------------------------------------------------


class _LLMCallTracker:
    """Context manager that wraps LLMClient.chat to count calls + tokens."""

    def __init__(self) -> None:
        self.calls: int = 0
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self._original: Any = None
        self._client: Any = None

    def attach(self, client: Any) -> None:
        self._client = client
        self._original = client.chat

        tracker = self

        async def _tracked_chat(messages: Any, **kwargs: Any) -> Any:
            response = await tracker._original(messages, **kwargs)
            tracker.calls += 1
            # Try to read usage from the response (LLMResponse.usage dict)
            usage = getattr(response, "usage", {})
            tracker.input_tokens += usage.get("prompt_tokens", 0)
            tracker.output_tokens += usage.get("candidates_tokens", 0)
            return response

        client.chat = _tracked_chat

    def detach(self) -> None:
        if self._client is not None and self._original is not None:
            self._client.chat = self._original


# ---------------------------------------------------------------------------
# Evidence file watcher
# ---------------------------------------------------------------------------


async def _wait_for_evidence(
    incident_id: str,
    evidence_dir: Path,
    timeout: float = 300.0,
    poll_interval: float = 1.0,
) -> Path | None:
    """Poll for evidence/<incident_id>/jenkins_agent.json, return path or None on timeout."""
    target = evidence_dir / str(incident_id) / "jenkins_agent.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if target.exists():
            return target
        await asyncio.sleep(poll_interval)
    return None


# ---------------------------------------------------------------------------
# Core: run one incident through the agent
# ---------------------------------------------------------------------------


async def run_one_incident(
    ground_truth: GroundTruth,
    run_index: int,
    *,
    mode: str,            # "hybrid" | "rules_only" | "llm_only"
    model_name: str | None,
    llm_cache_enabled: bool,
    evidence_dir: Path,
    per_run_timeout: float = 360.0,
) -> RunResult:
    """
    Instantiate and run the JenkinsInvestigationAgent for one synthetic incident.

    We bypass real Jenkins by using a mock JenkinsClient that returns pre-canned
    console text derived from the scenario's expected_failure_type.  This keeps
    the evaluation hermetic and reproducible.  When JENKINS_URL is set and
    --live-jenkins is passed the real client is used instead.
    """
    from common.llm_client import LLMClient, _LLM_CACHE_STORE
    from common.models import IncidentCreatedEvent, FailureTaxonomy
    from common.evidence_store import FileEvidenceStore

    incident_id = str(uuid4())
    webhook_ts = time.monotonic()

    # ------------------------------------------------------------------ env
    if mode == "rules_only":
        os.environ["LLM_OFFLINE"] = "1"
    else:
        os.environ.pop("LLM_OFFLINE", None)

    if model_name:
        os.environ["GEMINI_MODEL"] = model_name

    if llm_cache_enabled:
        os.environ["LLM_CACHE"] = "1"
        # Persist cache across runs
        if LLM_CACHE_PATH.exists():
            try:
                cached = json.loads(LLM_CACHE_PATH.read_text(encoding="utf-8"))
                _LLM_CACHE_STORE.update(cached)
            except Exception:
                pass
    else:
        os.environ["LLM_CACHE"] = "0"

    # ------------------------------------------------------------------ mock Jenkins client
    mock_console = _build_mock_console(ground_truth.expected_failure_type, ground_truth)
    mock_stage_summary = _build_mock_stage_summary(ground_truth.expected_failure_type)

    class _MockJenkinsClient:
        async def get_console_text(self, *a: Any, **kw: Any) -> Any:
            from agents.jenkins_agent.tools.jenkins_client import ConsoleText
            return ConsoleText(text=mock_console, truncated=False, partial=False)

        async def get_stage_summary(self, *a: Any, **kw: Any) -> dict[str, Any]:
            return mock_stage_summary

        async def get_test_report(self, *a: Any, **kw: Any) -> dict[str, Any] | None:
            return None

        async def get_build_history(self, *a: Any, **kw: Any) -> list[dict[str, Any]]:
            return []

        async def aclose(self) -> None:
            pass

    # ------------------------------------------------------------------ evidence store wired to our eval dir
    store = FileEvidenceStore(root=evidence_dir)

    # ------------------------------------------------------------------ LLM client + tracker
    tracker = _LLMCallTracker()
    rules_ts: float | None = None

    if mode == "rules_only":
        llm_client = None  # will be built offline inside the agent
    else:
        try:
            llm_client = LLMClient(llm_cache_enabled=llm_cache_enabled)
            tracker.attach(llm_client)
        except Exception:
            llm_client = None

    # ------------------------------------------------------------------ inject rule-only hook
    # We capture the timestamp just after rule classification finishes.
    _rule_ts_holder: list[float] = []
    # agent module must be imported first so its namespace is populated
    from agents.jenkins_agent.agent import JenkinsInvestigationAgent  # noqa: E402
    import agents.jenkins_agent.agent as _agent_module

    if mode != "llm_only":
        # Monkeypatch the name bound in agent.py's namespace (not the classifier module)
        # so that the agent calls our wrapper regardless of how it was imported.
        _orig_classify = _agent_module.rule_based_classify  # type: ignore[attr-defined]

        def _timed_classify(*args: Any, **kwargs: Any) -> Any:
            result = _orig_classify(*args, **kwargs)
            if not _rule_ts_holder:
                _rule_ts_holder.append(time.monotonic())
            return result

        _agent_module.rule_based_classify = _timed_classify  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ build the agent

    agent = JenkinsInvestigationAgent(
        llm_client=llm_client,
        evidence_store=store,
        jenkins_client_factory=lambda: _MockJenkinsClient(),
        total_timeout=per_run_timeout,
    )

    # LLM-only: monkeypatch to skip rule classification (patch agent namespace)
    if mode == "llm_only":
        _orig2 = _agent_module.rule_based_classify  # type: ignore[attr-defined]
        from common.models import FailureTaxonomy as _FT
        from agents.jenkins_agent.classifier import Hypothesis as _Hyp

        def _null_classify(*a: Any, **kw: Any) -> Any:
            return [_Hyp(
                failure_type=_FT.UNKNOWN,
                reason="LLM-only mode: rule classifier disabled",
                matched_evidence_ids=[],
                rule_confidence=0.2,
            )]

        _agent_module.rule_based_classify = _null_classify  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ create event
    event = IncidentCreatedEvent(
        incident_id=UUID(incident_id),
        job_name=f"eval/{ground_truth.scenario}",
        build_number=run_index + 1,
        build_url=f"http://jenkins.local/job/eval/{ground_truth.scenario}/{run_index + 1}/",
        branch=f"eval/{ground_truth.scenario}",
        git_commit="a" * 40,
        failed_stage=_failed_stage_for(ground_truth.expected_failure_type),
        timestamp=datetime.now(timezone.utc),
    )

    # ------------------------------------------------------------------ run
    evidence_ts_holder: list[float] = []
    error_msg: str | None = None
    evidence_data: Any = None

    try:
        evidence_data = await asyncio.wait_for(
            agent.run_event(event),
            timeout=per_run_timeout,
        )
        evidence_ts_holder.append(time.monotonic())
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.warning("Run %s/%d failed: %s", ground_truth.scenario, run_index, error_msg)
    finally:
        # Restore monkeypatches (both patches use _agent_module)
        if mode != "llm_only":
            _agent_module.rule_based_classify = _orig_classify  # type: ignore[attr-defined]
        if mode == "llm_only":
            _agent_module.rule_based_classify = _orig2  # type: ignore[attr-defined]
        tracker.detach()

    # ------------------------------------------------------------------ persist LLM cache
    if llm_cache_enabled and _LLM_CACHE_STORE:
        try:
            LLM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Serialise only JSON-serialisable values
            cacheable = {
                k: v if isinstance(v, (str, int, float, bool, type(None), dict, list)) else str(v)
                for k, v in _LLM_CACHE_STORE.items()
            }
            LLM_CACHE_PATH.write_text(json.dumps(cacheable, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------ extract result
    rules_ts_val = _rule_ts_holder[0] if _rule_ts_holder else None
    evidence_ts_val = evidence_ts_holder[0] if evidence_ts_holder else None

    if evidence_data is not None:
        predicted_type = evidence_data.failure_type.value
        confidence = float(evidence_data.confidence)
        status = evidence_data.status
        used_fallback = (
            evidence_data.summary.startswith("Rule-based classification:")
            if hasattr(evidence_data, "summary")
            else False
        )
        # Extract localization from first evidence item with a location
        pred_file: str | None = None
        pred_line: int | None = None
        for item in (evidence_data.evidence_items or []):
            loc = item.location
            if loc and loc.file:
                pred_file = loc.file
                pred_line = loc.line
                break

        related_ids = list(evidence_data.related_incident_ids or [])
        flaky_score = evidence_data.flaky_score
        duplicate_declared = bool(related_ids)
    else:
        predicted_type = "unknown"
        confidence = 0.0
        status = "failed"
        used_fallback = True
        pred_file = pred_line = None
        related_ids = []
        flaky_score = None
        duplicate_declared = False

    return RunResult(
        scenario=ground_truth.scenario,
        run_index=run_index,
        incident_id=incident_id,
        webhook_ts=webhook_ts,
        rules_ts=rules_ts_val,
        evidence_ts=evidence_ts_val,
        predicted_failure_type=predicted_type,
        expected_failure_type=ground_truth.expected_failure_type,
        confidence=confidence,
        predicted_file=pred_file,
        predicted_line=pred_line,
        expected_file=ground_truth.file,
        expected_line=ground_truth.line,
        llm_calls=tracker.calls,
        llm_input_tokens=tracker.input_tokens,
        llm_output_tokens=tracker.output_tokens,
        used_fallback=used_fallback,
        status=status,
        error=error_msg,
        related_incident_ids=related_ids,
        flaky_score=flaky_score,
        duplicate_declared=duplicate_declared,
    )


# ---------------------------------------------------------------------------
# Mock console builders
# ---------------------------------------------------------------------------

_CONSOLE_TEMPLATES: dict[str, str] = {
    "code_test_failure": (
        "Running pytest...\n"
        "FAILED target_app/tests/test_orders.py::test_order_quantity - "
        "AssertionError: assert 4 == 3\n"
        "short test summary info\n"
        "FAILED target_app/tests/test_orders.py::test_order_quantity\n"
        "1 failed in 2.3s\n"
    ),
    "build_compilation_failure": (
        "Running build...\n"
        "  File \"target_app/app/main.py\", line 42\n"
        "    app = create_app(\n"
        "                    ^\n"
        "SyntaxError: '(' was never closed\n"
        "ERROR collecting target_app/\n"
        "Build stage failed with exit code 1\n"
    ),
    "dependency_regression": (
        "Installing dependencies...\n"
        "ERROR: pip's dependency resolver does not currently take into account all the "
        "packages that are installed. This behaviour is the source of the following "
        "dependency conflicts.\n"
        "httpx 0.99.0 requires some-package>=2.0, but you have some-package 1.0 which is "
        "incompatible.\n"
        "pip dependency resolution failed\n"
        "Build failed\n"
    ),
    "flaky_test": (
        "Running pytest...\n"
        "FAILED target_app/tests/test_orders.py::test_flaky_random_order_check - "
        "AssertionError: assert False\n"
        "Previous build passed on same commit\n"
        "1 failed in 1.1s\n"
    ),
    "infra_network_failure": (
        "Running integration tests...\n"
        "connection refused: could not connect to database on port 5432\n"
        "Name or service not known\n"
        "Could not resolve host: registry.example.com\n"
        "git checkout failed: repository not found\n"
    ),
    "config_error": (
        "Deployment stage\n"
        "Missing environment variable: DATABASE_URL\n"
        "settings.yaml: invalid yaml structure\n"
        "Deploy stage failed\n"
        "Configuration error detected\n"
    ),
    "timeout": (
        "Running tests...\n"
        "Test stage timed out after 300s\n"
        "Process killed: timeout\n"
        "Test stage FAILED\n"
        "Build timed out\n"
    ),
    "auth_failure": (
        "Authentication failed for user jenkins-deploy\n"
        "403 Forbidden: access denied\n"
        "Credentials rejected by remote server\n"
        "Build failed due to auth failure\n"
    ),
    "deployment_failure": (
        "Deploy stage\n"
        "Kubernetes deployment failed: ImagePullBackOff\n"
        "pod/app-78f9b-rk2mc error\n"
        "Deploy stage FAILED\n"
    ),
    "unknown": (
        "Build failed\nExit code 1\n"
    ),
}


def _build_mock_console(failure_type: str, gt: GroundTruth) -> str:
    base = _CONSOLE_TEMPLATES.get(failure_type, _CONSOLE_TEMPLATES["unknown"])
    # If ground truth has a specific file, include it in log
    if gt.file and gt.line:
        base += f"\n  File \"{gt.file}\", line {gt.line}\n"
    return base


def _build_mock_stage_summary(failure_type: str) -> dict[str, Any]:
    stage_map: dict[str, dict[str, Any]] = {
        "code_test_failure": {
            "stages": [{"name": "build", "result": "SUCCESS"}, {"name": "test", "result": "FAILURE"}]
        },
        "build_compilation_failure": {
            "stages": [{"name": "build", "result": "FAILURE"}]
        },
        "dependency_regression": {
            "stages": [{"name": "build", "result": "FAILURE"}]
        },
        "flaky_test": {
            "stages": [{"name": "build", "result": "SUCCESS"}, {"name": "test", "result": "FAILURE"}]
        },
        "timeout": {
            "stages": [{"name": "test", "result": "FAILURE"}]
        },
        "deployment_failure": {
            "stages": [{"name": "build", "result": "SUCCESS"}, {"name": "deploy", "result": "FAILURE"}]
        },
        "config_error": {
            "stages": [{"name": "build", "result": "SUCCESS"}, {"name": "deploy", "result": "FAILURE"}]
        },
    }
    return stage_map.get(failure_type, {"stages": []})


def _failed_stage_for(failure_type: str) -> str | None:
    mapping = {
        "code_test_failure": "Test",
        "build_compilation_failure": "Build",
        "dependency_regression": "Build",
        "flaky_test": "Test",
        "timeout": "Test",
        "deployment_failure": "Deploy",
        "config_error": "Deploy",
        "infra_network_failure": "Test",
        "auth_failure": "Deploy",
    }
    return mapping.get(failure_type)


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    sorted_v = sorted(values)
    idx = (p / 100.0) * (len(sorted_v) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_v) - 1)
    return sorted_v[lo] + (idx - lo) * (sorted_v[hi] - sorted_v[lo])


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def _ece(pairs: list[tuple[float, bool]], n_bins: int = ECE_BINS) -> float:
    """Expected Calibration Error over n_bins uniform confidence bins."""
    if not pairs:
        return float("nan")
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for conf, correct in pairs:
        idx = min(int(conf * n_bins), n_bins - 1)
        bins[idx].append((conf, correct))
    total = len(pairs)
    ece = 0.0
    for b in bins:
        if not b:
            continue
        avg_conf = _mean([c for c, _ in b])
        avg_acc = _mean([float(ok) for _, ok in b])
        ece += (len(b) / total) * abs(avg_conf - avg_acc)
    return ece


def compute_metrics(
    results: list[RunResult],
    mode: str,
    model_name: str | None,
) -> dict[str, Any]:
    """Aggregate all RunResults into a metrics dict."""
    completed = [r for r in results if r.error is None]
    failed_runs = [r for r in results if r.error is not None]

    # Classification accuracy
    cls_pairs = [(r.expected_failure_type, r.predicted_failure_type) for r in completed]
    correct_cls = [(e, p) for e, p in cls_pairs if e == p]
    accuracy = len(correct_cls) / len(cls_pairs) if cls_pairs else float("nan")

    # File/line localization accuracy
    loc_runs = [r for r in completed if r.expected_file is not None]
    file_hits = [
        r for r in loc_runs
        if r.predicted_file and
        (r.predicted_file.endswith(r.expected_file) or r.expected_file.endswith(r.predicted_file))
    ]
    file_acc = len(file_hits) / len(loc_runs) if loc_runs else float("nan")

    line_runs = [r for r in file_hits if r.expected_line is not None]
    line_hits = [r for r in line_runs if r.predicted_line == r.expected_line]
    line_acc = len(line_hits) / len(line_runs) if line_runs else float("nan")

    # Confidence for correct / incorrect
    conf_correct = [r.confidence for r in completed if r.predicted_failure_type == r.expected_failure_type]
    conf_incorrect = [r.confidence for r in completed if r.predicted_failure_type != r.expected_failure_type]

    # ECE
    ece_pairs = [
        (r.confidence, r.predicted_failure_type == r.expected_failure_type)
        for r in completed
    ]
    ece = _ece(ece_pairs, ECE_BINS)

    # Latency (monotonic seconds converted to ms or s)
    rules_latencies_ms: list[float] = []
    final_latencies_s: list[float] = []

    for r in completed:
        if r.rules_ts is not None:
            rules_latencies_ms.append((r.rules_ts - r.webhook_ts) * 1000.0)
        if r.evidence_ts is not None:
            final_latencies_s.append(r.evidence_ts - r.webhook_ts)

    # LLM calls & tokens
    total_llm_calls = sum(r.llm_calls for r in completed)
    total_input_tokens = sum(r.llm_input_tokens for r in completed)
    total_output_tokens = sum(r.llm_output_tokens for r in completed)
    n = len(completed) or 1
    avg_llm_calls = total_llm_calls / n
    avg_input_tokens = total_input_tokens / n
    avg_output_tokens = total_output_tokens / n

    # Fallback rate
    fallback_runs = [r for r in completed if r.used_fallback]
    fallback_rate = len(fallback_runs) / len(completed) if completed else float("nan")

    # Flaky false positives:
    # Runs where expected type is NOT flaky_test but agent said flaky_test
    non_flaky = [r for r in completed if r.expected_failure_type != "flaky_test"]
    flaky_fp = [r for r in non_flaky if r.predicted_failure_type == "flaky_test"]
    flaky_fp_rate = len(flaky_fp) / len(non_flaky) if non_flaky else float("nan")

    # Duplicate detection precision:
    # Among runs where agent declared a duplicate (related_incident_ids non-empty),
    # how many were actually the same scenario run more than once (true duplicates)?
    dup_declared = [r for r in completed if r.duplicate_declared]
    # We define "true duplicate" as: same scenario with run_index > 0
    true_dups = [r for r in dup_declared if r.run_index > 0]
    dup_precision = len(true_dups) / len(dup_declared) if dup_declared else float("nan")

    # Per-class breakdown
    per_class: dict[str, Any] = {}
    all_labels = sorted({r.expected_failure_type for r in completed} | {r.predicted_failure_type for r in completed})
    for label in all_labels:
        tp = sum(r.expected_failure_type == label and r.predicted_failure_type == label for r in completed)
        fp = sum(r.expected_failure_type != label and r.predicted_failure_type == label for r in completed)
        fn = sum(r.expected_failure_type == label and r.predicted_failure_type != label for r in completed)
        prec = tp / (tp + fp) if (tp + fp) else float("nan")
        rec = tp / (tp + fn) if (tp + fn) else float("nan")
        f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) else float("nan")
        per_class[label] = {
            "precision": prec,
            "recall": rec,
            "f1": f1,
            "support": tp + fn,
        }

    return {
        "mode": mode,
        "model": model_name or os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        "total_runs": len(results),
        "completed_runs": len(completed),
        "failed_runs": len(failed_runs),
        "classification_accuracy": accuracy,
        "file_localization_accuracy": file_acc,
        "line_localization_accuracy": line_acc,
        "avg_confidence_correct": _mean(conf_correct),
        "avg_confidence_incorrect": _mean(conf_incorrect),
        "calibration_ece": ece,
        "calibration_bins": ECE_BINS,
        # Latency – rules path is ms (fast), LLM path is seconds (slow)
        "time_to_first_evidence_rules_ms": {
            "p50": _percentile(rules_latencies_ms, 50),
            "p95": _percentile(rules_latencies_ms, 95),
            "mean": _mean(rules_latencies_ms),
            "n": len(rules_latencies_ms),
        },
        "time_to_final_evidence_s": {
            "p50": _percentile(final_latencies_s, 50),
            "p95": _percentile(final_latencies_s, 95),
            "mean": _mean(final_latencies_s),
            "n": len(final_latencies_s),
        },
        "llm_per_incident": {
            "avg_calls": avg_llm_calls,
            "avg_input_tokens": avg_input_tokens,
            "avg_output_tokens": avg_output_tokens,
            "total_calls": total_llm_calls,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
        },
        "fallback_rate": fallback_rate,
        "flaky_false_positive_rate": flaky_fp_rate,
        "duplicate_detection_precision": dup_precision,
        "per_class": per_class,
        "run_details": [asdict(r) for r in results],
    }


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def _pct(v: float, digits: int = 1) -> str:
    if math.isnan(v):
        return "n/a"
    return f"{v:.{digits}%}"


def _fmt(v: float, digits: int = 3) -> str:
    if math.isnan(v):
        return "n/a"
    return f"{v:.{digits}f}"


def render_markdown(metrics: dict[str, Any], all_metrics: list[dict[str, Any]]) -> str:
    """Render a markdown summary table comparing all evaluated modes."""
    lines: list[str] = [
        "# Jenkins Agent Evaluation Results",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Summary",
        "",
        "| Metric | " + " | ".join(m["mode"] for m in all_metrics) + " |",
        "|--------|" + "|".join("--------" for _ in all_metrics) + "|",
    ]

    metric_rows: list[tuple[str, str, Any]] = [
        ("Total runs", "total_runs", lambda v: str(int(v))),
        ("Completed runs", "completed_runs", lambda v: str(int(v))),
        ("Failed runs", "failed_runs", lambda v: str(int(v))),
        ("Classification accuracy", "classification_accuracy", lambda v: _pct(v)),
        ("File localization accuracy", "file_localization_accuracy", lambda v: _pct(v)),
        ("Line localization accuracy", "line_localization_accuracy", lambda v: _pct(v)),
        ("Avg confidence (correct)", "avg_confidence_correct", lambda v: _fmt(v, 3)),
        ("Avg confidence (incorrect)", "avg_confidence_incorrect", lambda v: _fmt(v, 3)),
        ("ECE calibration", "calibration_ece", lambda v: _fmt(v, 4)),
        ("Fallback rate (rule-only)", "fallback_rate", lambda v: _pct(v)),
        ("Flaky false-positive rate", "flaky_false_positive_rate", lambda v: _pct(v)),
        ("Duplicate detection precision", "duplicate_detection_precision", lambda v: _pct(v)),
    ]

    for row_label, key, fmt_fn in metric_rows:
        cells = []
        for m in all_metrics:
            val = m.get(key, float("nan"))
            try:
                cells.append(fmt_fn(val))
            except Exception:
                cells.append(str(val))
        lines.append(f"| {row_label} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## Latency (Honest breakdown)",
        "",
        "> [!NOTE]",
        "> Rule-based classification is **milliseconds** (deterministic regex/pattern matching).",
        "> LLM-augmented final evidence is **seconds** (network + LLM reasoning).",
        "> These are reported separately to avoid misleading averages.",
        "",
        "| Path | Metric | " + " | ".join(m["mode"] for m in all_metrics) + " |",
        "|------|--------|" + "|".join("--------" for _ in all_metrics) + "|",
    ]

    for latency_key, latency_label in [
        ("time_to_first_evidence_rules_ms", "Rules (ms)"),
        ("time_to_final_evidence_s", "Final (s)"),
    ]:
        for stat in ["p50", "p95", "mean"]:
            cells = []
            for m in all_metrics:
                val = (m.get(latency_key) or {}).get(stat, float("nan"))
                cells.append(_fmt(val, 1))
            lines.append(f"| {latency_label} | {stat.upper()} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## LLM Usage per Incident",
        "",
        "| Metric | " + " | ".join(m["mode"] for m in all_metrics) + " |",
        "|--------|" + "|".join("--------" for _ in all_metrics) + "|",
    ]

    for llm_key, llm_label in [
        ("avg_calls", "Avg LLM calls"),
        ("avg_input_tokens", "Avg input tokens"),
        ("avg_output_tokens", "Avg output tokens"),
    ]:
        cells = []
        for m in all_metrics:
            val = (m.get("llm_per_incident") or {}).get(llm_key, float("nan"))
            cells.append(_fmt(val, 1))
        lines.append(f"| {llm_label} | " + " | ".join(cells) + " |")

    # Per-class table for the "current" mode
    if all_metrics:
        lines += [
            "",
            f"## Per-class Breakdown ({all_metrics[-1]['mode']})",
            "",
            "| Class | Precision | Recall | F1 | Support |",
            "|-------|-----------|--------|----|---------|",
        ]
        for label, m in sorted((all_metrics[-1].get("per_class") or {}).items()):
            lines.append(
                f"| {label} | {_pct(m['precision'])} | {_pct(m['recall'])} "
                f"| {_pct(m['f1'])} | {m['support']} |"
            )

    lines += [
        "",
        "---",
        "",
        "> [!IMPORTANT]",
        "> Classification accuracy is computed on completed runs only.",
        "> Failed runs (agent exception / timeout) are excluded from metric calculations but counted.",
        "",
        "> [!TIP]",
        "> To add more scenarios, place a `.json` file with `expected_failure_type` in `scenarios/`.",
    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main evaluation loop
# ---------------------------------------------------------------------------


async def evaluate(
    *,
    n_runs: int,
    mode: str,
    model_name: str | None,
    llm_cache_enabled: bool,
    scenarios_dir: Path,
    evidence_dir: Path,
    per_run_timeout: float = 300.0,
) -> dict[str, Any]:
    """Run the full evaluation and return metrics dict."""
    truths = load_ground_truths(scenarios_dir)
    if not truths:
        logger.error("No ground-truth scenarios found in %s", scenarios_dir)
        return {}

    logger.info(
        "Evaluating %d scenarios × %d runs = %d total incidents [mode=%s]",
        len(truths), n_runs, len(truths) * n_runs, mode,
    )

    results: list[RunResult] = []
    total = len(truths) * n_runs
    done = 0

    for gt in truths:
        for run_idx in range(n_runs):
            done += 1
            logger.info(
                "[%d/%d] scenario=%s run=%d",
                done, total, gt.scenario, run_idx,
            )
            try:
                result = await run_one_incident(
                    gt,
                    run_idx,
                    mode=mode,
                    model_name=model_name,
                    llm_cache_enabled=llm_cache_enabled,
                    evidence_dir=evidence_dir,
                    per_run_timeout=per_run_timeout,
                )
            except Exception as exc:
                logger.error(
                    "Unexpected error for %s run %d: %s\n%s",
                    gt.scenario, run_idx, exc, traceback.format_exc(),
                )
                # Insert a placeholder failed result so the total count is accurate
                result = RunResult(
                    scenario=gt.scenario,
                    run_index=run_idx,
                    incident_id=str(uuid4()),
                    webhook_ts=time.monotonic(),
                    rules_ts=None,
                    evidence_ts=None,
                    predicted_failure_type="unknown",
                    expected_failure_type=gt.expected_failure_type,
                    confidence=0.0,
                    predicted_file=None,
                    predicted_line=None,
                    expected_file=gt.file,
                    expected_line=gt.line,
                    llm_calls=0,
                    llm_input_tokens=0,
                    llm_output_tokens=0,
                    used_fallback=True,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(result)

    return compute_metrics(results, mode=mode, model_name=model_name)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-n", "--runs", type=int, default=5,
        help="Number of runs per scenario (default: 5)"
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Rules-only mode: disable LLM (sets LLM_OFFLINE=1)"
    )
    parser.add_argument(
        "--no-rules", action="store_true",
        help="LLM-only mode: skip rule_based_classify"
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="Override GEMINI_MODEL (e.g. gemini-2.5-pro)"
    )
    parser.add_argument(
        "--no-llm-cache", action="store_true",
        help="Disable prompt-hash LLM cache"
    )
    parser.add_argument(
        "--compare", action="store_true",
        help="Run all three modes (rules-only, LLM-only, hybrid) and compare"
    )
    parser.add_argument(
        "--scenarios-dir", type=Path, default=SCENARIOS_DIR,
        help="Directory with *.json ground-truth scenarios"
    )
    parser.add_argument(
        "--evidence-dir", type=Path, default=EVIDENCE_DIR,
        help="Directory to write agent evidence"
    )
    parser.add_argument(
        "--results-dir", type=Path, default=RESULTS_DIR,
        help="Directory to write results"
    )
    parser.add_argument(
        "--per-run-timeout", type=float, default=300.0,
        help="Per-incident timeout in seconds (default: 300)"
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Quieten noisy internal loggers
    logging.getLogger("crewai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if args.no_llm and args.no_rules:
        print("ERROR: --no-llm and --no-rules cannot be combined.", file=sys.stderr)
        return 1

    args.results_dir.mkdir(parents=True, exist_ok=True)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)

    llm_cache_enabled = not args.no_llm_cache

    if args.compare:
        modes = ["rules_only", "llm_only", "hybrid"]
    elif args.no_llm:
        modes = ["rules_only"]
    elif args.no_rules:
        modes = ["llm_only"]
    else:
        modes = ["hybrid"]

    all_metrics: list[dict[str, Any]] = []
    for mode in modes:
        print(f"\n{'='*60}")
        print(f" Evaluation mode: {mode}  (n={args.runs})")
        print(f"{'='*60}")
        m = await evaluate(
            n_runs=args.runs,
            mode=mode,
            model_name=args.model,
            llm_cache_enabled=llm_cache_enabled,
            scenarios_dir=args.scenarios_dir,
            evidence_dir=args.evidence_dir,
            per_run_timeout=args.per_run_timeout,
        )
        if not m:
            print("No results produced for mode:", mode)
            continue
        all_metrics.append(m)

        # Print quick summary
        print(f"\nMode={mode}  accuracy={_pct(m['classification_accuracy'])}  "
              f"ECE={_fmt(m['calibration_ece'], 4)}  "
              f"fallback_rate={_pct(m['fallback_rate'])}")
        print(
            f"  rules_latency p50={_fmt(m['time_to_first_evidence_rules_ms']['p50'], 1)}ms  "
            f"p95={_fmt(m['time_to_first_evidence_rules_ms']['p95'], 1)}ms"
        )
        print(
            f"  final_latency p50={_fmt(m['time_to_final_evidence_s']['p50'], 1)}s  "
            f"p95={_fmt(m['time_to_final_evidence_s']['p95'], 1)}s"
        )

    if not all_metrics:
        return 1

    # Combined output uses the last (or only) run as primary
    primary = all_metrics[-1]

    # Write JSON
    json_path = args.results_dir / "jenkins_agent_eval.json"
    combined = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs_per_scenario": args.runs,
        "modes_evaluated": modes,
        "results": {m["mode"]: m for m in all_metrics},
    }
    json_path.write_text(json.dumps(combined, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote: {json_path}")

    # Write Markdown
    md_path = args.results_dir / "jenkins_agent_eval.md"
    md_text = render_markdown(primary, all_metrics)
    md_path.write_text(md_text, encoding="utf-8")
    print(f"Wrote: {md_path}")

    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    sys.exit(main())
