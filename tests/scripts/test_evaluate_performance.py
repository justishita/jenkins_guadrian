"""scripts/evaluate_performance.py: the statistics, the audit pairing, the estimate, and that it never calls an LLM."""

import json
import runpy
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

SCRIPT = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "evaluate_performance.py"))
quantile, describe, box_svg = SCRIPT["quantile"], SCRIPT["describe"], SCRIPT["box_svg"]
recorded_runs, prompt_estimate, load_variants, load_logs = SCRIPT["recorded_runs"], SCRIPT["prompt_estimate"], SCRIPT["load_variants"], SCRIPT["load_logs"]
main = SCRIPT["main"]


def event(actor: str, kind: str, when: str, **extra) -> dict:
    return {"actor": actor, "event_type": kind, "created_at": when, "duration_ms": extra.pop("duration_ms", None), "payload": extra.pop("payload", {}), **extra}


def write_audit(folder: Path, incident: str, events: list[dict]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{incident}.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")


def test_quantiles_interpolate_and_do_not_need_sorted_input() -> None:
    assert quantile([4, 1, 3, 2], 0.5) == 2.5
    assert quantile([10], 0.95) == 10
    assert quantile([1, 2, 3, 4, 5], 0.0) == 1 and quantile([1, 2, 3, 4, 5], 1.0) == 5


def test_describe_of_nothing_says_n_zero_rather_than_inventing_numbers() -> None:
    assert describe([]) == {"n": 0}
    stats = describe([1.0, 2.0, 3.0])
    assert stats["n"] == 3 and stats["median"] == 2.0 and stats["min"] == 1.0 and stats["max"] == 3.0


def test_a_start_and_its_end_give_one_run_time(tmp_path: Path) -> None:
    write_audit(tmp_path, "aaaa", [
        event("metrics_agent", "agent_started", "2026-11-17T10:00:00Z"),
        event("metrics_agent", "tool_call", "2026-11-17T10:00:01Z", duration_ms=12.5),
        event("metrics_agent", "agent_completed", "2026-11-17T10:00:03Z"),
    ])
    result = recorded_runs(tmp_path)
    assert result["agent_seconds"] == {"metrics_agent": [3.0]}
    assert result["tool_ms"] == {"metrics_agent": [12.5]}
    assert result["unfinished_runs_excluded"] == 0


def test_a_run_that_died_is_excluded_not_counted_as_fast_or_as_days_long(tmp_path: Path) -> None:
    """A start with no end, retried two days later: the run time is not "two days", and the dead run is counted."""
    write_audit(tmp_path, "bbbb", [
        event("metrics_agent", "agent_started", "2026-11-14T22:56:58Z"),
        event("metrics_agent", "agent_started", "2026-11-16T17:14:48Z"),
        event("metrics_agent", "agent_completed", "2026-11-16T17:15:07Z"),
    ])
    result = recorded_runs(tmp_path)
    assert result["agent_seconds"] == {"metrics_agent": [19.0]}
    assert result["unfinished_runs_excluded"] == 1


def test_a_start_that_never_ends_is_counted_as_unfinished(tmp_path: Path) -> None:
    write_audit(tmp_path, "cccc", [event("code_agent", "agent_started", "2026-11-17T10:00:00Z")])
    result = recorded_runs(tmp_path)
    assert result["agent_seconds"] == {} and result["unfinished_runs_excluded"] == 1


def test_only_the_first_report_counts_for_time_to_report(tmp_path: Path) -> None:
    write_audit(tmp_path, "dddd", [
        event("backend", "incident_created", "2026-11-17T10:00:00Z"),
        event("coordinator", "synthesis_completed", "2026-11-17T10:00:20Z", payload={"attempt": 0}),
        event("coordinator", "synthesis_completed", "2026-11-17T10:05:00Z", payload={"attempt": 0}),
    ])
    assert recorded_runs(tmp_path)["end_to_end_seconds"] == [20.0]


def test_the_box_plot_is_valid_svg_and_survives_one_point_or_no_data() -> None:
    ET.fromstring(box_svg({"a": [1, 2, 3, 4, 100], "b": [5]}, title="t", unit="s"))
    ET.fromstring(box_svg({"a": [0.01, 1.0, 50.0]}, title="t", unit="s", log_scale=True))
    ET.fromstring(box_svg({}, title="t", unit="s"))
    ET.fromstring(box_svg({"same": [2.0, 2.0, 2.0]}, title="t", unit="s"))


def test_every_prompt_variant_on_disk_is_estimated_and_the_logs_include_the_labelled_ones() -> None:
    names = {v["name"] for v in load_variants()}
    assert {"react", "plan_execute", "persona_adversarial"} <= names
    logs = load_logs()
    assert len(logs) >= 10 and sum(1 for log in logs if log["expected"]) >= 4


def test_the_estimate_grows_with_turns_and_with_the_log_and_costs_the_planning_variant_more() -> None:
    variants = {v["name"]: v for v in load_variants()}
    logs = sorted(load_logs(), key=lambda log: len(log["text"]))
    small, big = logs[0], logs[-1]
    price = dict(out_tokens=400, price_in=0.3, price_out=2.5)
    two = prompt_estimate(variants["react"], big, turns=2, **price)
    four = prompt_estimate(variants["react"], big, turns=4, **price)
    assert four["usd"] > two["usd"] and four["input_tokens"] > two["input_tokens"]
    assert prompt_estimate(variants["react"], big, turns=3, **price)["usd"] > prompt_estimate(variants["react"], small, turns=3, **price)["usd"]
    assert prompt_estimate(variants["plan_execute"], small, turns=3, **price)["usd"] > prompt_estimate(variants["react"], small, turns=3, **price)["usd"]


def test_a_free_model_costs_nothing_and_a_dearer_price_costs_more() -> None:
    variant, log = load_variants()[0], load_logs()[0]
    assert prompt_estimate(variant, log, turns=3, out_tokens=400, price_in=0.0, price_out=0.0)["usd"] == 0.0
    cheap = prompt_estimate(variant, log, turns=3, out_tokens=400, price_in=0.3, price_out=2.5)["usd"]
    dear = prompt_estimate(variant, log, turns=3, out_tokens=400, price_in=3.0, price_out=25.0)["usd"]
    assert dear == pytest.approx(cheap * 10)


def test_the_whole_script_runs_on_empty_data_without_a_network_or_an_llm(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    out = tmp_path / "out"
    code = main([
        "--audit-dir", str(tmp_path / "audit"), "--evidence-dir", str(tmp_path / "evidence"),
        "--blackboard-dir", str(tmp_path / "bb"), "--out", str(out), "--repeats", "1",
    ])
    assert code == 0
    report = (out / "performance_eval.md").read_text(encoding="utf-8")
    assert "no LLM was called" in report and "ESTIMATED" in report
    assert {p.name for p in out.iterdir()} == {
        "performance_eval.md", "performance_eval.json", "performance_runs.csv", "performance_agent_runtime.svg", "performance_prompt_cost.svg",
    }
    ET.fromstring((out / "performance_prompt_cost.svg").read_text(encoding="utf-8"))


def test_dispatch_comparison_uses_the_sum_for_sequential_and_the_slowest_for_parallel() -> None:
    compare_dispatch = SCRIPT["compare_dispatch"]
    rows = [("inc1", "a", 2.0), ("inc1", "b", 3.0), ("inc1", "c", 10.0), ("inc2", "a", 1.0), ("inc2", "b", 1.0), ("inc2", "c", 1.0),
            ("inc3", "a", 5.0), ("inc3", "b", 5.0)]  # inc3 has only two agents timed: not comparable
    result = compare_dispatch(rows)
    assert result["incidents"] == 2 and result["skipped_incomplete"] == 1
    assert sorted(result["sequential"]) == [3.0, 15.0] and sorted(result["parallel"]) == [1.0, 10.0]
    assert sorted(result["saved"]) == [2.0, 5.0]
    assert result["slowest_agent"]["c"] >= 1


def test_dispatch_comparison_of_nothing_is_empty_not_an_error() -> None:
    result = SCRIPT["compare_dispatch"]([])
    assert result["incidents"] == 0 and result["sequential"] == [] and result["rows"] == []
