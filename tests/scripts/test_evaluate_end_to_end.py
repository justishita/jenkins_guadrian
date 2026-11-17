"""scripts/evaluate_end_to_end.py: ground truth, scoring, honest intervals and the shape of a miss."""

import json
import runpy
from pathlib import Path

import pytest

SCRIPT = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "evaluate_end_to_end.py"))
build_sample, score, wilson, render_markdown = SCRIPT["build_sample"], SCRIPT["score"], SCRIPT["wilson"], SCRIPT["render_markdown"]
parse_incident_rows, scenario_of, branch_of, main = SCRIPT["parse_incident_rows"], SCRIPT["scenario_of"], SCRIPT["branch_of"], SCRIPT["main"]
EXPECTED = SCRIPT["EXPECTED"]


def finding(agent, failure_type, confidence=0.8, abstained=False):
    return {"agent": agent, "failure_type": failure_type, "confidence": confidence, "abstained": abstained, "status": "completed", "summary": f"{agent} says {failure_type}"}


def write_report(folder: Path, incident: str, verdict: str, findings: list[dict], *, conflicting=False, revision=1, attempt=0):
    path = folder / incident
    path.mkdir(parents=True, exist_ok=True)
    report = {
        "incident_id": incident, "failure_type": verdict, "confidence": 0.72, "status": "conflicting" if conflicting else "resolved_cause",
        "summary": "s", "findings": findings, "conflicts": [{"failure_type": "x", "claimed_by": ["jenkins_agent"], "confidence": 0.7}] if conflicting else [],
        "missing_agents": [], "agent_outcomes": {a: "completed" for a in ("jenkins_agent", "metrics_agent", "code_agent")},
        "hypotheses": [], "created_at": "2026-11-16T10:00:00Z",
    }
    (path / f"a{attempt}-r{revision}.json").write_text(
        json.dumps({"incident_id": incident, "attempt": attempt, "revision": revision, "report_revision_id": f"{incident}:{attempt}", "created_at": "2026-11-16T10:00:00Z", "report": report}),
        encoding="utf-8",
    )


def row(incident: str, scenario: str, build=1):
    return {"id": incident, "branch": f"fault/{scenario}-20261116T100000000000Z", "job_name": "target-app/x", "build": build, "created_at": "2026-11-16 10:00:00+00"}


def three(jenkins, metrics, code, **abstain):
    return [finding("jenkins_agent", jenkins, abstained=abstain.get("j", False)), finding("metrics_agent", metrics, abstained=abstain.get("m", False)), finding("code_agent", code, abstained=abstain.get("c", False))]


@pytest.fixture
def reports(tmp_path):
    return tmp_path / "reports"


def test_ground_truth_matches_the_injector_for_all_four_faults():
    assert EXPECTED == {
        "tc01_unit_test": "code_test_failure", "tc02_compile_syntax": "build_compilation_failure",
        "tc02_compile_import": "dependency_regression", "tc10_flaky": "flaky_test",
    }


def test_the_scenario_comes_from_the_branch_name_or_the_url_encoded_job_name():
    assert scenario_of("fault/tc01_unit_test-20261030T064927905681Z") == "tc01_unit_test"
    assert scenario_of("fault/tc02_compile_import-20261030T064927905681Z") == "tc02_compile_import"
    assert scenario_of("main") is None
    assert branch_of({"branch": "", "job_name": "target-app/fault%2Ftc10_flaky-20261030T064927905681Z"}) == "fault/tc10_flaky-20261030T064927905681Z"


def test_psql_rows_are_parsed_and_junk_lines_ignored():
    rows = parse_incident_rows("abc|fault/tc01_unit_test-20261030T064927905681Z|target-app/f|3|2026-10-30 06:57:50+00\n\nnot a row\n")
    assert rows == [{"id": "abc", "branch": "fault/tc01_unit_test-20261030T064927905681Z", "job_name": "target-app/f", "build": 3, "created_at": "2026-10-30 06:57:50+00"}]


def test_a_right_verdict_scores_correct_and_each_agent_is_judged_separately(reports):
    write_report(reports, "i1", "code_test_failure", three("code_test_failure", "unknown", "code_test_failure", m=True))
    sample = build_sample(row("i1", "tc01_unit_test"), reports)
    assert sample.status == "scored" and sample.correct is True
    assert sample.agents["jenkins_agent"].correct is True
    assert sample.agents["metrics_agent"].abstained and sample.agents["metrics_agent"].correct is None  # abstaining is not wrong


def test_the_first_live_miss_is_recorded_with_who_was_wrong(reports):
    """The 30 Oct tc01 run: Jenkins said dependency_regression, Code abstained, the Coordinator said infra_network_failure."""
    write_report(reports, "i1", "infra_network_failure", three("dependency_regression", "infra_network_failure", "unknown", c=True), conflicting=True)
    sample = build_sample(row("i1", "tc01_unit_test"), reports)
    assert sample.correct is False and sample.conflicted is True
    assert [sample.agents[a].correct for a in ("jenkins_agent", "metrics_agent")] == [False, False]
    assert sample.agents["code_agent"].abstained
    result = score([sample])
    assert result["misses"]["no_agent_was_right"] == 1 and result["misses"]["agent_was_right_but_overruled"] == 0


def test_a_miss_where_an_agent_was_right_is_counted_as_overruled_by_the_fusion(reports):
    write_report(reports, "i1", "timeout", three("code_test_failure", "timeout", "unknown", c=True))
    result = score([build_sample(row("i1", "tc01_unit_test"), reports)])
    assert result["misses"]["agent_was_right_but_overruled"] == 1 and result["any_agent_right"]["correct"] == 1


def test_an_incident_without_a_report_is_pending_and_not_scored(reports):
    sample = build_sample(row("i1", "tc01_unit_test"), reports)
    result = score([sample])
    assert sample.status == "pending" and result["scored"] == 0 and result["pending"] == ["i1"]


def test_an_unknown_branch_is_set_aside_not_scored(reports):
    sample = build_sample({"id": "i9", "branch": "fault/not_a_scenario-20261116T100000000000Z", "job_name": "j", "build": 1, "created_at": "x"}, reports)
    assert sample.status == "unknown_scenario" and score([sample])["unknown_scenario"] == ["i9"]


def test_only_the_first_attempts_initial_report_is_scored_and_a_later_change_is_noted(reports):
    write_report(reports, "i1", "timeout", three("timeout", "timeout", "timeout"), revision=1)
    write_report(reports, "i1", "code_test_failure", three("timeout", "timeout", "timeout"), revision=2)
    write_report(reports, "i1", "flaky_test", three("timeout", "timeout", "timeout"), attempt=1, revision=1)  # a re-investigation: ignored
    sample = build_sample(row("i1", "tc01_unit_test"), reports)
    assert sample.verdict == "timeout" and sample.correct is False  # what a human saw first
    assert sample.revised_to == "code_test_failure"


def test_wrong_answers_are_counted_separately_from_right_ones(reports):
    write_report(reports, "a", "dependency_regression", three("dependency_regression", "unknown", "dependency_regression", m=True))
    write_report(reports, "b", "code_test_failure", three("code_test_failure", "unknown", "code_test_failure", m=True))
    samples = [build_sample(row("a", "tc01_unit_test"), reports), build_sample(row("b", "tc01_unit_test"), reports)]
    result = score(samples)
    assert result["coordinator"]["correct"] == 1 and result["misses"]["total"] == 1
    assert result["misses"]["wrong_and_actionable"] == (1 if samples[0].actionable else 0)


def test_wilson_interval_is_wide_for_few_samples_and_tight_for_many():
    low, high = wilson(1, 1)
    assert low < 0.25 and high == 1.0  # one right answer proves almost nothing
    low, high = wilson(95, 100)
    assert 0.88 < low < 0.92 and 0.97 < high < 0.99
    assert wilson(0, 0) == (0.0, 0.0)


def test_the_markdown_leads_with_the_small_sample_warning_and_lists_every_miss(reports):
    write_report(reports, "deadbeef-0", "infra_network_failure", three("dependency_regression", "infra_network_failure", "unknown", c=True), conflicting=True)
    samples = [build_sample(row("deadbeef-0", "tc01_unit_test"), reports)]
    text = render_markdown(samples, score(samples), "2026-11-16 10:00 UTC")
    assert "Small sample (1)" in text and "Every miss" in text
    assert "deadbeef" in text and "infra_network_failure" in text and "WRONG" in text and "abstained" in text


def test_the_command_writes_json_and_markdown_without_docker(reports, tmp_path, capsys):
    write_report(reports, "i1", "code_test_failure", three("code_test_failure", "unknown", "code_test_failure", m=True))
    incidents = tmp_path / "incidents.json"
    incidents.write_text(json.dumps([row("i1", "tc01_unit_test")]), encoding="utf-8")
    out = tmp_path / "out" / "e2e"
    assert main(["--incidents-file", str(incidents), "--reports-dir", str(reports), "--out", str(out)]) == 0
    saved = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert saved["result"]["coordinator"]["correct"] == 1 and out.with_suffix(".md").read_text(encoding="utf-8").startswith("# End-to-end accuracy")
    assert "scored 1 incident" in capsys.readouterr().out


def test_nothing_to_score_is_reported_not_crashed(reports, tmp_path, capsys):
    incidents = tmp_path / "none.json"
    incidents.write_text("[]", encoding="utf-8")
    assert main(["--incidents-file", str(incidents), "--reports-dir", str(reports), "--out", str(tmp_path / "x")]) == 1
    assert "inject some" in capsys.readouterr().out


# ---- --branches: score exactly the branches a run injected -----------------------------------------------------


def test_branches_file_limits_scoring_and_reports_branches_that_never_made_an_incident(reports, tmp_path, capsys):
    write_report(reports, "keep", "code_test_failure", three("code_test_failure", "unknown", "code_test_failure", m=True))
    write_report(reports, "stray", "timeout", three("timeout", "timeout", "timeout"))
    keep, stray = row("keep", "tc01_unit_test"), row("stray", "tc02_compile_syntax")
    incidents = tmp_path / "incidents.json"
    incidents.write_text(json.dumps([keep, stray]), encoding="utf-8")
    never = "fault/tc10_flaky-20261116T100000000000Z"
    branches = tmp_path / "fault_branches.txt"
    branches.write_text("﻿" + keep["branch"] + "\n\n" + never + "\n", encoding="utf-8")  # a PowerShell file starts with a BOM
    out = tmp_path / "out" / "e2e"
    assert main(["--incidents-file", str(incidents), "--reports-dir", str(reports), "--branches", str(branches), "--out", str(out)]) == 0
    saved = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert [s["incident_id"] for s in saved["samples"]] == ["keep"]  # the stray fault branch is not scored
    assert saved["result"]["branches_without_incident"] == [never]
    assert never in out.with_suffix(".md").read_text(encoding="utf-8")
    assert "branches with no incident yet" in capsys.readouterr().out


def test_without_a_branches_file_every_fault_incident_is_scored(reports, tmp_path):
    write_report(reports, "a", "code_test_failure", three("code_test_failure", "unknown", "code_test_failure", m=True))
    write_report(reports, "b", "timeout", three("timeout", "timeout", "timeout"))
    incidents = tmp_path / "incidents.json"
    incidents.write_text(json.dumps([row("a", "tc01_unit_test"), row("b", "tc01_unit_test")]), encoding="utf-8")
    out = tmp_path / "e2e"
    assert main(["--incidents-file", str(incidents), "--reports-dir", str(reports), "--out", str(out)]) == 0
    assert json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))["result"]["scored"] == 2


def test_a_branches_file_written_all_on_one_line_still_reads(reports, tmp_path):
    """A broken writer once put every branch name on one line; the scorer must not match nothing because of it."""
    write_report(reports, "a", "code_test_failure", three("code_test_failure", "unknown", "code_test_failure", m=True))
    write_report(reports, "b", "build_compilation_failure", three("build_compilation_failure", "build_compilation_failure", "build_compilation_failure"))
    a, b = row("a", "tc01_unit_test"), row("b", "tc02_compile_syntax")
    incidents = tmp_path / "incidents.json"
    incidents.write_text(json.dumps([a, b]), encoding="utf-8")
    branches = tmp_path / "one_line.txt"
    branches.write_text(a["branch"] + " " + b["branch"], encoding="utf-8")
    out = tmp_path / "e2e"
    assert main(["--incidents-file", str(incidents), "--reports-dir", str(reports), "--branches", str(branches), "--out", str(out)]) == 0
    assert json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))["result"]["scored"] == 2
