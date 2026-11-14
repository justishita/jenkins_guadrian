"""scripts/evaluate_coordinator.py: the methods, the metrics, the seeded simulation and the report."""

import json
import random
import runpy
from pathlib import Path

import pytest

SCRIPT = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "evaluate_coordinator.py"))
Spec, Decision, T = SCRIPT["Spec"], SCRIPT["Decision"], SCRIPT["T"]
METHODS, authoritative, noisy_or = SCRIPT["METHODS"], SCRIPT["authoritative"], SCRIPT["noisy_or"]
highest_confidence, majority_vote = SCRIPT["highest_confidence"], SCRIPT["majority_vote"]
ece_and_brier, summarize = SCRIPT["ece_and_brier"], SCRIPT["summarize"]
simulate_case, run_simulation, run_labelled, labelled_cases = SCRIPT["simulate_case"], SCRIPT["run_simulation"], SCRIPT["run_labelled"], SCRIPT["labelled_cases"]
findings, render_markdown, conditions, main = SCRIPT["findings"], SCRIPT["render_markdown"], SCRIPT["conditions"], SCRIPT["main"]


def agree(failure_type=T.TIMEOUT, confidence=0.8):
    return {agent: Spec(failure_type, confidence) for agent in ("jenkins_agent", "metrics_agent", "code_agent")}


def test_every_method_returns_the_shared_verdict_when_all_agents_agree():
    for name, method in METHODS.items():
        decision = method(agree())
        if name in ("only_jenkins", "only_metrics", "only_code") or name in ("authoritative", "noisy_or", "highest_confidence", "majority_vote"):
            assert decision.verdict == "timeout", name
    assert authoritative(agree()).acts is True


def test_abstaining_agents_do_not_vote_in_the_baselines():
    inputs = {"jenkins_agent": Spec(T.UNKNOWN, 0.3, "insufficient_evidence"), "metrics_agent": Spec(T.TIMEOUT, 0.7), "code_agent": Spec(T.UNKNOWN, 0.2, "insufficient_evidence")}
    assert highest_confidence(inputs).verdict == "timeout" and majority_vote(inputs).verdict == "timeout"
    nobody = {"jenkins_agent": Spec(T.UNKNOWN, 0.3, "insufficient_evidence")}
    assert highest_confidence(nobody) == Decision("unknown", 0.0, False) and majority_vote(nobody) == Decision("unknown", 0.0, False)


def test_highest_confidence_believes_the_single_most_confident_agent_and_majority_the_plurality():
    inputs = {"jenkins_agent": Spec(T.TIMEOUT, 0.6), "metrics_agent": Spec(T.TIMEOUT, 0.6), "code_agent": Spec(T.CONFIG_ERROR, 0.9)}
    assert highest_confidence(inputs).verdict == "config_error"
    assert majority_vote(inputs).verdict == "timeout" and majority_vote(inputs).conflicted is True


def test_a_failed_agent_or_a_missing_one_makes_the_authoritative_report_not_actionable():
    failed = {"jenkins_agent": Spec(T.UNKNOWN, 0.0, "failed"), "metrics_agent": Spec(T.TIMEOUT, 0.8), "code_agent": Spec(T.TIMEOUT, 0.8)}
    assert authoritative(failed).acts is False  # a fix must not rest on a partial investigation
    two = {"metrics_agent": Spec(T.TIMEOUT, 0.8), "code_agent": Spec(T.TIMEOUT, 0.8)}
    assert authoritative(two).acts is False and authoritative(two).verdict == "timeout"


def test_the_baselines_keep_acting_when_the_authoritative_coordinator_refuses():
    """A single agent's confidence is enough for the baselines, which is exactly what the completeness rule exists to prevent."""
    only_one = {"metrics_agent": Spec(T.TIMEOUT, 0.8)}
    assert highest_confidence(only_one).acts is True and authoritative(only_one).acts is False


def test_noisy_or_honours_a_scenarios_own_fusion_settings():
    scenario = next(case for case in labelled_cases() if case[0] == "coord-compatible-close")
    _, truth, inputs, overrides = scenario
    assert overrides == {"trust_proposed_relations": True} and truth == "resource_exhaustion"
    assert noisy_or(inputs, overrides).verdict == truth


def test_calibration_metrics_on_known_values():
    assert ece_and_brier([(1.0, True), (0.0, False)]) == (0.0, 0.0)
    assert ece_and_brier([(0.9, False)]) == (0.9, 0.81)
    assert ece_and_brier([]) == (None, None)


def test_summary_counts_wrong_and_acting_and_keeps_ambiguous_cases_out_of_accuracy():
    cases = [
        ("timeout", Decision("timeout", 0.9, True)),            # right, acting
        ("timeout", Decision("config_error", 0.8, True, True)),  # wrong, acting, conflicted
        ("timeout", Decision("unknown", 0.2, False)),            # wrong, not acting
        (None, Decision("timeout", 0.9, True)),                  # ambiguous: acting on it is counted separately
    ]
    s = summarize(cases)
    assert s["n"] == 3 and s["accuracy"] == pytest.approx(1 / 3, abs=1e-4)
    assert s["acts"] == pytest.approx(2 / 3, abs=1e-4) and s["precision_when_acting"] == 0.5
    assert s["wrong_and_acting"] == pytest.approx(1 / 3, abs=1e-4) and s["acting_despite_conflict"] == 0.5
    assert (s["ambiguous_n"], s["ambiguous_acted_on"]) == (1, 1)


def test_the_labelled_set_is_the_thirteen_coordinator_scenarios_ten_of_them_labelled():
    result = run_labelled()
    assert result["cases"] == 13 and result["labelled"] == 10
    assert set(result["per_method"]) == set(METHODS)
    assert all(case[2] for case in labelled_cases())  # every scenario has agent inputs


def test_the_simulation_is_deterministic_for_a_seed_and_differs_between_seeds():
    assert run_simulation(20, seed=1) == run_simulation(20, seed=1)
    assert run_simulation(20, seed=1) != run_simulation(20, seed=2)


def test_the_simulation_covers_every_condition_and_method():
    simulated = run_simulation(5, seed=3)
    assert len(simulated) == len(conditions()) == 12
    assert all(set(per) == set(METHODS) for per in simulated.values())


def test_a_perfect_agent_model_mostly_names_the_true_cause():
    rng = random.Random(0)
    right = sum(
        1
        for _ in range(200)
        for spec in simulate_case(rng, T.TIMEOUT, 1.0, 0.0, lambda agent, cause: 1.0).values()
        if spec.failure_type is T.TIMEOUT
    )
    assert right > 0.9 * 600  # 98% per agent


def test_shared_errors_make_the_wrong_agents_agree():
    rng = random.Random(0)
    agreeing = 0
    for _ in range(300):
        wrong = [s.failure_type for s in simulate_case(rng, T.TIMEOUT, 0.0, 1.0, lambda a, c: 0.0).values() if not s.abstained]
        agreeing += len(wrong) > 1 and len(set(wrong)) == 1
    assert agreeing > 0  # misled the same way: the case where corroboration is misleading
    rng = random.Random(0)
    independent = sum(
        1
        for _ in range(300)
        if len({s.failure_type for s in simulate_case(rng, T.TIMEOUT, 0.0, 0.0, lambda a, c: 0.0).values() if not s.abstained}) > 1
    )
    assert independent > 0


def test_findings_are_computed_from_the_numbers():
    lines = findings(run_simulation(30, seed=4))
    assert any("single agent" in line for line in lines) and any("Worst case" in line for line in lines)


def test_the_report_states_that_the_simulation_is_not_measured_accuracy():
    text = render_markdown(run_labelled(), run_simulation(5, seed=1), 5, 1, "2026-11-16 10:00 UTC")
    assert "This is a simulation of the inputs, not measured accuracy of the real agents." in text
    assert "## A. The labelled scenarios" in text and "## B. Simulation" in text and "## Limits" in text
    assert "Acts on ambiguous" in text


def test_the_command_writes_json_and_markdown(tmp_path, capsys):
    out = tmp_path / "coordinator_eval"
    assert main(["--n", "10", "--seed", "5", "--out", str(out)]) == 0
    saved = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert saved["n"] == 10 and "authoritative" in saved["labelled"]["per_method"] and len(saved["simulation"]) == 12
    assert out.with_suffix(".md").read_text(encoding="utf-8").startswith("# Coordinator evaluation")
    assert "labelled scenarios: authoritative accuracy" in capsys.readouterr().out


def test_a_non_positive_sample_size_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        main(["--n", "0", "--out", str(tmp_path / "x")])
