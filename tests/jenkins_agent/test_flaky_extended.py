"""Extended unit tests for agents/jenkins_agent/tools/flaky.py."""

import json
import pytest

from agents.jenkins_agent.tools.flaky import (
    BuildTestRecord,
    CheckFlakinessTool,
    FlakinessReport,
    _extract_cases_from_report,
    _extract_commit_from_build,
    _main_job_fallback,
    _matches_test,
    create_flakiness_tool,
    score_from_records,
)


def test_flakiness_report_to_json():
    record = BuildTestRecord(build_number=1, passed=True, commit="abc1234", duration_ms=100.0)
    report = FlakinessReport(
        test_name="test_login",
        job="target-app/main",
        pass_count=1,
        fail_count=0,
        total_runs=1,
        transitions=0,
        same_commit_both_passed_and_failed=False,
        regression_detected=False,
        regression_commit=None,
        flaky_score=0.0,
        is_flaky=False,
        status="stable",
        confidence=0.9,
        history=[record],
        details="Stable test",
    )
    raw = report.to_json()
    parsed = json.loads(raw)
    assert parsed["test_name"] == "test_login"
    assert parsed["history"][0]["commit"] == "abc1234"


def test_extract_cases_from_report():
    assert _extract_cases_from_report(None) == []
    assert _extract_cases_from_report({}) == []

    report = {
        "cases": [{"name": "case1", "className": "Class1"}],
        "suites": [
            {"cases": [{"name": "case2", "className": "Class2"}]}
        ],
        "childReports": [
            {"result": {"suites": [{"cases": [{"name": "case3", "className": "Class3"}]}]}}
        ],
    }
    cases = _extract_cases_from_report(report)
    assert len(cases) == 3
    names = [c["name"] for c in cases]
    assert names == ["case1", "case2", "case3"]


def test_matches_test_renames_and_variants():
    assert _matches_test("test_checkout_v1", "TestClass", "test_checkout_v2")
    assert _matches_test("test_checkout_new", "TestClass", "test_checkout_old")
    assert _matches_test("test_something[param1]", "TestClass", "test_something[param2]")
    assert not _matches_test("test_foo", "TestClass", "test_bar")


def test_extract_commit_from_build():
    assert _extract_commit_from_build(None) is None
    assert _extract_commit_from_build({}) is None
    assert _extract_commit_from_build({"commit": "commit_direct"}) == "commit_direct"
    assert _extract_commit_from_build({"git_commit": "git_commit_direct"}) == "git_commit_direct"

    # From actions.buildsByBranchName
    build_with_branch_action = {
        "actions": [
            {
                "buildsByBranchName": {
                    "origin/main": {"SHA1": "sha_from_branch"}
                }
            }
        ]
    }
    assert _extract_commit_from_build(build_with_branch_action) == "sha_from_branch"

    # From changeSet
    build_with_changeset = {
        "changeSet": {
            "items": [{"commitId": "sha_from_changeset"}]
        }
    }
    assert _extract_commit_from_build(build_with_changeset) == "sha_from_changeset"


def test_main_job_fallback_sequences():
    assert _main_job_fallback(("org", "repo", "feature-x")) == ["org", "repo", "main"]
    assert _main_job_fallback(("org", "repo", "main")) is None


def test_check_flakiness_tool_without_client():
    tool = CheckFlakinessTool(job="target-app/main", client=None)
    output = tool._run("test_checkout")
    data = json.loads(output)
    assert "error" in data
    assert data["is_flaky"] is False


def test_create_flakiness_tool():
    class DummyClient:
        pass

    tool = create_flakiness_tool("target-app/feature", DummyClient())
    assert tool.name == "check_flakiness"
    assert tool.job == "target-app/feature"
