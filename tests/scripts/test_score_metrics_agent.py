from pathlib import Path
from typing import Any

import pytest

from common.llm_client import MockLLMClient
from common.models import FailureTaxonomy
from scripts.score_metrics_agent import (
    LLMRunner,
    LLMVerdict,
    Thresholds,
    baseline_predict,
    llm_prompt,
    summarize,
)
from tests.metrics_agent.scenario_runner import load_metrics_scenarios


def _scenario(name: str) -> dict[str, Any]:
    return next(s for s in load_metrics_scenarios() if s["_file"] == f"{name}.yaml")


def _verdict(failure_type: FailureTaxonomy = FailureTaxonomy.RESOURCE_EXHAUSTION) -> LLMVerdict:
    return LLMVerdict(failure_type=failure_type, confidence=0.9, reasoning="cpu high")


def test_prompt_shows_the_series_but_never_the_ground_truth() -> None:
    scenario = _scenario("metrics_cpu_burn")
    prompt = llm_prompt(scenario)

    assert "cpu_rate" in prompt and "0.9" in prompt  # the incident-window values are there
    # Nothing from the scenario's expectation or prose may leak into the question.
    for leak in (scenario["name"], scenario["scenario"].split()[0] + " " + scenario["scenario"].split()[1], "expected"):
        assert leak not in prompt
    assert "min_confidence" not in prompt


def test_prompt_reports_unavailable_prometheus_and_missing_data() -> None:
    assert "query failed" in llm_prompt(_scenario("metrics_prometheus_unavailable"))
    assert "no data returned" in llm_prompt(_scenario("metrics_no_data"))


def test_runner_returns_the_verdict_and_caches_it(tmp_path: Path) -> None:
    client = MockLLMClient([_verdict()])
    runner = LLMRunner(client, tmp_path / "cache.json")
    scenario = _scenario("metrics_cpu_burn")

    assert runner.predict(scenario) == ("resource_exhaustion", 0.9)
    assert runner.predict(scenario) == ("resource_exhaustion", 0.9)  # served from cache: mock has no 2nd response
    assert len(client.calls) == 1

    # A fresh runner on the same file makes no call at all.
    again = MockLLMClient([])
    assert LLMRunner(again, tmp_path / "cache.json").predict(scenario) == ("resource_exhaustion", 0.9)
    assert again.calls == []


def test_runner_accepts_a_plain_dict_answer(tmp_path: Path) -> None:
    client = MockLLMClient([{"failure_type": "timeout", "confidence": 0.7, "reasoning": "slow"}])
    assert LLMRunner(client, tmp_path / "c.json").predict(_scenario("metrics_latency_spike")) == ("timeout", 0.7)


@pytest.mark.parametrize("bad", [RuntimeError("quota"), {"failure_type": "made_up", "confidence": 2, "reasoning": ""}])
def test_failed_or_invalid_calls_are_counted_not_scored(tmp_path: Path, bad: Any) -> None:
    runner = LLMRunner(MockLLMClient([bad]), tmp_path / "c.json")

    assert runner.predict(_scenario("metrics_healthy")) is None
    assert runner.errors == 1
    assert not (tmp_path / "c.json").exists()  # a failure is never cached as an answer


def test_threshold_baseline_has_no_baseline_period_and_trips_on_a_glitch() -> None:
    t = Thresholds()
    assert baseline_predict(_scenario("metrics_healthy"), t) == "unknown"
    assert baseline_predict(_scenario("metrics_cpu_burn"), t) == "resource_exhaustion"
    assert baseline_predict(_scenario("metrics_single_sample_glitch"), t) == "timeout"


def test_summary_separates_false_positives_from_missed_faults() -> None:
    pairs = [("timeout", "timeout"), ("timeout", "unknown"), ("unknown", "timeout"), ("unknown", "unknown")]
    s = summarize(pairs)

    assert s["accuracy"] == 0.5
    assert s["fault_recall"] == 0.5
    assert s["false_positive_rate"] == 0.5
