"""Unit tests for historical test-flakiness scoring and Jenkins history collection."""

from __future__ import annotations

import json

import pytest

from agents.jenkins_agent.tools.flaky import (
    FLAKY_RECOMMENDATION,
    FLAKY_SCORE_THRESHOLD,
    BuildTestRecord,
    CheckFlakinessTool,
    analyze_test_flakiness,
    create_flakiness_tool,
    detect_regression_point,
    score_from_records,
)


def _records(outcomes: list[tuple[int, bool, str | None]]) -> list[BuildTestRecord]:
    return [
        BuildTestRecord(build_number=number, passed=passed, commit=commit)
        for number, passed, commit in outcomes
    ]


def test_consistently_failing_test_is_not_flaky() -> None:
    records = _records([(n, False, "abc") for n in range(1, 8)])
    report = score_from_records("test_always_fails", "target-app/main", records)

    assert report.transitions == 0
    assert report.fail_count == 7
    assert report.pass_count == 0
    assert report.flaky_score == 0.0
    assert report.is_flaky is False
    assert report.status == "consistently_failing"


def test_first_build_ever_is_insufficient_history() -> None:
    report = score_from_records("test_new", "target-app/main", [])

    assert report.status == "insufficient_history"
    assert report.is_flaky is False
    assert report.flaky_score == 0.0
    assert report.total_runs == 0


def test_single_failing_build_is_not_flaky() -> None:
    report = score_from_records(
        "test_new",
        "target-app/main",
        [BuildTestRecord(build_number=1, passed=False, commit="aaa")],
    )

    assert report.status == "insufficient_history"
    assert report.is_flaky is False


def test_fewer_than_five_builds_is_insufficient_history() -> None:
    records = _records(
        [
            (1, True, "a"),
            (2, False, "a"),
            (3, True, "a"),
            (4, False, "a"),
        ]
    )
    report = score_from_records("test_x", "target-app/main", records)

    assert report.status == "insufficient_history"
    assert report.is_flaky is False
    assert report.confidence <= 0.2


def test_genuine_regression_after_commit_is_not_flaky() -> None:
    records = _records(
        [
            (1, True, "aaa"),
            (2, True, "aaa"),
            (3, False, "bbb"),
            (4, False, "bbb"),
            (5, False, "ccc"),
            (6, False, "ccc"),
        ]
    )
    is_regression, commit = detect_regression_point(records)
    report = score_from_records("test_orders", "target-app/main", records)

    assert is_regression is True
    assert commit == "bbb"
    assert report.status == "regression"
    assert report.regression_detected is True
    assert report.regression_commit == "bbb"
    assert report.is_flaky is False
    assert report.flaky_score <= 0.15
    assert report.flaky_score < FLAKY_SCORE_THRESHOLD


def test_oscillating_results_are_flaky() -> None:
    records = _records(
        [
            (1, True, "aaa"),
            (2, False, "aaa"),
            (3, True, "bbb"),
            (4, False, "bbb"),
            (5, True, "ccc"),
            (6, False, "ccc"),
        ]
    )
    report = score_from_records("test_flaky", "target-app/main", records)

    assert report.transitions == 5
    assert report.same_commit_both_passed_and_failed is True
    assert report.status == "flaky"
    assert report.is_flaky is True
    assert report.flaky_score > FLAKY_SCORE_THRESHOLD
    assert report.to_dict()["recommendation"] == FLAKY_RECOMMENDATION


def test_same_commit_pass_and_fail_boosts_score_not_regression() -> None:
    records = _records(
        [
            (1, True, "same"),
            (2, True, "same"),
            (3, False, "same"),
            (4, False, "same"),
            (5, True, "same"),
            (6, False, "same"),
        ]
    )
    is_regression, _ = detect_regression_point(records)
    report = score_from_records("test_race", "target-app/main", records)

    assert is_regression is False
    assert report.same_commit_both_passed_and_failed is True
    assert report.is_flaky is True
    assert report.regression_detected is False


class _FakeJenkins:
    def __init__(
        self,
        history: dict[str, list[dict]],
        reports: dict[tuple[str, int], dict | None],
        builds: dict[tuple[str, int], dict] | None = None,
    ) -> None:
        self.history = history
        self.reports = reports
        self.builds = builds or {}
        self.history_jobs: list[str] = []

    async def get_build_history(self, job, limit: int = 20):
        job_str = job if isinstance(job, str) else "/".join(job)
        self.history_jobs.append(job_str)
        return list(self.history.get(job_str, []))[:limit]

    async def get_build(self, job, number: int):
        job_str = job if isinstance(job, str) else "/".join(job)
        return self.builds.get((job_str, number), {"number": number})

    async def get_test_report(self, job, number: int):
        job_str = job if isinstance(job, str) else "/".join(job)
        return self.reports.get((job_str, number))


def _suite_report(name: str, status: str, class_name: str = "tests.test_orders") -> dict:
    return {
        "suites": [
            {
                "cases": [
                    {"name": name, "className": class_name, "status": status, "duration": 0.01}
                ]
            }
        ]
    }


@pytest.mark.asyncio
async def test_analyze_skips_builds_without_test_reports() -> None:
    client = _FakeJenkins(
        history={
            "target-app/main": [
                {"number": n, "commit": "c1"} for n in range(1, 8)
            ]
        },
        reports={
            ("target-app/main", 1): _suite_report("test_x", "PASSED"),
            ("target-app/main", 2): None,
            ("target-app/main", 3): _suite_report("test_x", "FAILED"),
            ("target-app/main", 4): _suite_report("test_x", "PASSED"),
            ("target-app/main", 5): None,
            ("target-app/main", 6): _suite_report("test_x", "FAILED"),
            ("target-app/main", 7): _suite_report("test_x", "PASSED"),
        },
    )

    report = await analyze_test_flakiness("target-app/main", "test_x", client)

    assert report.total_runs == 5
    assert report.pass_count == 3
    assert report.fail_count == 2
    assert report.transitions >= 1


@pytest.mark.asyncio
async def test_analyze_matches_renamed_tests() -> None:
    client = _FakeJenkins(
        history={"target-app/main": [{"number": n, "commit": "c"} for n in range(1, 7)]},
        reports={
            ("target-app/main", n): _suite_report(
                "test_checkout_v1" if n < 4 else "test_checkout_v2",
                "PASSED" if n % 2 else "FAILED",
            )
            for n in range(1, 7)
        },
    )

    report = await analyze_test_flakiness("target-app/main", "test_checkout_v2", client)

    assert report.total_runs == 6
    assert report.pass_count + report.fail_count == 6


@pytest.mark.asyncio
async def test_branch_falls_back_to_main_when_history_is_short() -> None:
    client = _FakeJenkins(
        history={
            "target-app/feature/flaky": [{"number": 1, "commit": "b1"}],
            "target-app/main": [
                {"number": n, "commit": "m"} for n in range(10, 16)
            ],
        },
        reports={
            ("target-app/feature/flaky", 1): _suite_report("test_x", "FAILED"),
            **{
                ("target-app/main", n): _suite_report("test_x", "PASSED" if n % 2 else "FAILED")
                for n in range(10, 16)
            },
        },
    )

    report = await analyze_test_flakiness("target-app/feature/flaky", "test_x", client)

    assert "target-app/main" in client.history_jobs
    assert report.total_runs >= 5


def test_check_flakiness_tool_name_and_unconfigured_client() -> None:
    tool = CheckFlakinessTool()
    assert tool.name == "check_flakiness"
    payload = json.loads(tool._run("test_orders"))
    assert payload["is_flaky"] is False
    assert payload["flaky_score"] == 0.0

    wired = create_flakiness_tool("target-app/main", client=object())
    assert wired.name == "check_flakiness"
    assert wired.job == "target-app/main"
