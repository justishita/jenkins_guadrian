"""Flakiness analysis tool for Jenkins pipeline test failures.

Fetches build history and test reports across recent builds (default N=20) to compute:
- Pass count, fail count, and transition count (pass <-> fail)
- Whether failures occurred across identical git commits
- Regression point detection (fails consistently from commit X onwards)
- Flaky score in [0, 1]

Handles:
- Builds without test reports
- Renamed tests
- Fewer than 5 builds of history ('insufficient_history')
- Branch builds falling back to main for baseline
- Consistently failing tests (transitions=0 -> not flaky)
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from agents.jenkins_agent.tools.jenkins_client import JenkinsClient, JobPath

try:
    from crewai.tools import BaseTool
except ImportError:  # pragma: no cover - scoring can run without CrewAI installed
    class BaseTool:  # type: ignore[no-redef]
        name: str = ""
        description: str = ""

        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

logger = logging.getLogger(__name__)

MIN_BUILDS_FOR_HISTORY = 5
DEFAULT_HISTORY_LIMIT = 20
FLAKY_SCORE_THRESHOLD = 0.40
SAME_COMMIT_BOOST = 0.35
FLAKY_RECOMMENDATION = "retry / quarantine and investigate"


@dataclass(frozen=True, slots=True)
class BuildTestRecord:
    build_number: int
    passed: bool
    commit: str | None = None
    duration_ms: float | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_number": self.build_number,
            "passed": self.passed,
            "commit": self.commit,
            "duration_ms": self.duration_ms,
            "error_message": self.error_message,
        }


@dataclass
class FlakinessReport:
    """Diagnostic report on historical test flakiness."""

    test_name: str
    job: str
    pass_count: int
    fail_count: int
    total_runs: int
    transitions: int
    same_commit_both_passed_and_failed: bool
    regression_detected: bool
    regression_commit: str | None
    flaky_score: float
    is_flaky: bool
    status: str  # "flaky" | "consistently_failing" | "regression" | "insufficient_history" | "stable"
    confidence: float
    history: list[BuildTestRecord]
    details: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_name": self.test_name,
            "job": self.job,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "total_runs": self.total_runs,
            "transitions": self.transitions,
            "same_commit_both_passed_and_failed": self.same_commit_both_passed_and_failed,
            "regression_detected": self.regression_detected,
            "regression_commit": self.regression_commit,
            "flaky_score": self.flaky_score,
            "is_flaky": self.is_flaky,
            "status": self.status,
            "confidence": self.confidence,
            "history": [r.to_dict() for r in self.history],
            "details": self.details,
            "recommendation": FLAKY_RECOMMENDATION if self.is_flaky else None,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def _extract_cases_from_report(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract all test case dictionaries from a Jenkins test report JSON."""
    if not report or not isinstance(report, dict):
        return []

    cases: list[dict[str, Any]] = []

    if isinstance(report.get("cases"), list):
        cases.extend(c for c in report["cases"] if isinstance(c, dict))

    for suite in report.get("suites", []):
        if isinstance(suite, dict) and isinstance(suite.get("cases"), list):
            cases.extend(c for c in suite["cases"] if isinstance(c, dict))

    for child in report.get("childReports", []):
        if isinstance(child, dict):
            res = child.get("result", {})
            if isinstance(res, dict):
                for suite in res.get("suites", []):
                    if isinstance(suite, dict) and isinstance(suite.get("cases"), list):
                        cases.extend(c for c in suite["cases"] if isinstance(c, dict))

    return cases


def _matches_test(case_name: str, case_class: str, target_name: str) -> bool:
    """Match a case against a target test name, supporting renames and path prefixes."""
    case_name = case_name.strip()
    case_class = case_class.strip()
    target_name = target_name.strip()

    if target_name in {case_name, f"{case_class}.{case_name}", f"{case_class}::{case_name}"}:
        return True

    target_base = target_name.split("::")[-1].split(".")[-1].split("[")[0].strip()
    case_base = case_name.split("[")[0].strip()
    if target_base == case_base:
        return True

    # Handle renamed tests (test_checkout_v1 vs test_checkout_v2 / _old / _new)
    clean_target = re.sub(r"(_v\d+|_new|_old|_renamed)$", "", target_base, flags=re.IGNORECASE)
    clean_case = re.sub(r"(_v\d+|_new|_old|_renamed)$", "", case_base, flags=re.IGNORECASE)
    if clean_target and clean_target == clean_case:
        return True

    return False


def _extract_commit_from_build(build: dict[str, Any]) -> str | None:
    """Extract git commit SHA from Jenkins build dict or actions."""
    if not isinstance(build, dict):
        return None
    if build.get("commit"):
        return str(build["commit"])
    if build.get("git_commit"):
        return str(build["git_commit"])

    for action in build.get("actions", []):
        if not isinstance(action, dict):
            continue
        last_built = action.get("lastBuiltRevision")
        if isinstance(last_built, dict) and last_built.get("SHA1"):
            return str(last_built["SHA1"])
        if "buildsByBranchName" in action and isinstance(action["buildsByBranchName"], dict):
            for b_info in action["buildsByBranchName"].values():
                if isinstance(b_info, dict) and b_info.get("SHA1"):
                    return str(b_info["SHA1"])

    change_set = build.get("changeSet")
    if isinstance(change_set, dict):
        items = change_set.get("items", [])
        if items and isinstance(items[0], dict) and items[0].get("commitId"):
            return str(items[0]["commitId"])

    return None


def _same_commit_inconsistency(records: list[BuildTestRecord]) -> bool:
    commit_outcomes: dict[str, set[bool]] = defaultdict(set)
    for record in records:
        if record.commit:
            commit_outcomes[record.commit].add(record.passed)
    return any(len(outcomes) > 1 for outcomes in commit_outcomes.values())


def count_transitions(records: list[BuildTestRecord]) -> int:
    return sum(
        1 for i in range(len(records) - 1) if records[i].passed != records[i + 1].passed
    )


def detect_regression_point(records: list[BuildTestRecord]) -> tuple[bool, str | None]:
    """Detect if failure began at a specific commit and persisted continuously.

    A genuine regression is a single pass→fail flip after which every later
    run failed. Same-commit pass/fail is flakiness, not a regression.
    """
    if len(records) < 2:
        return False, None
    if _same_commit_inconsistency(records):
        return False, None
    if not records[0].passed or records[-1].passed:
        return False, None

    first_fail_idx = next((i for i, r in enumerate(records) if not r.passed), None)
    if first_fail_idx is None or first_fail_idx == 0:
        return False, None
    if not all(not r.passed for r in records[first_fail_idx:]):
        return False, None
    # Exactly one transition (the regression), not oscillating history
    if count_transitions(records) != 1:
        return False, None

    return True, records[first_fail_idx].commit


def _empty_report(
    test_name: str,
    job: str,
    *,
    status: str,
    details: str,
    confidence: float,
    records: list[BuildTestRecord] | None = None,
) -> FlakinessReport:
    records = records or []
    return FlakinessReport(
        test_name=test_name,
        job=job,
        pass_count=sum(1 for r in records if r.passed),
        fail_count=sum(1 for r in records if not r.passed),
        total_runs=len(records),
        transitions=0,
        same_commit_both_passed_and_failed=False,
        regression_detected=False,
        regression_commit=None,
        flaky_score=0.0,
        is_flaky=False,
        status=status,
        confidence=confidence,
        history=records,
        details=details,
    )


def score_from_records(
    test_name: str,
    job: str,
    records: list[BuildTestRecord],
) -> FlakinessReport:
    """Compute a flakiness report from already-collected per-build test outcomes."""
    ordered = sorted(records, key=lambda r: r.build_number)
    total_runs = len(ordered)
    if total_runs < MIN_BUILDS_FOR_HISTORY:
        details = (
            "First build ever or no build history found; cannot determine flakiness."
            if total_runs == 0
            else (
                f"Fewer than {MIN_BUILDS_FOR_HISTORY} builds of test history available "
                f"({total_runs} found); insufficient evidence for flakiness."
            )
        )
        return _empty_report(
            test_name,
            job,
            status="insufficient_history",
            details=details,
            confidence=0.10 if total_runs == 0 else 0.20,
            records=ordered,
        )

    pass_count = sum(1 for r in ordered if r.passed)
    fail_count = sum(1 for r in ordered if not r.passed)
    transitions = count_transitions(ordered)
    same_commit = _same_commit_inconsistency(ordered)
    is_regression, regression_commit = detect_regression_point(ordered)

    if transitions == 0:
        if fail_count > 0 and pass_count == 0:
            status = "consistently_failing"
            details = "A consistently failing test is not flaky (transitions = 0)."
        else:
            status = "stable"
            details = "Test passed consistently across all inspected builds."
        return FlakinessReport(
            test_name=test_name,
            job=job,
            pass_count=pass_count,
            fail_count=fail_count,
            total_runs=total_runs,
            transitions=0,
            same_commit_both_passed_and_failed=same_commit,
            regression_detected=False,
            regression_commit=None,
            flaky_score=0.0,
            is_flaky=False,
            status=status,
            confidence=0.90,
            history=ordered,
            details=details,
        )

    base_score = transitions / (total_runs - 1)
    flaky_score = min(1.0, base_score)
    if same_commit:
        flaky_score = min(1.0, flaky_score + SAME_COMMIT_BOOST)

    if is_regression:
        # Genuine regression after commit X: reduce score so it is not treated as flaky
        flaky_score = round(min(flaky_score * 0.15, 0.15), 4)
        return FlakinessReport(
            test_name=test_name,
            job=job,
            pass_count=pass_count,
            fail_count=fail_count,
            total_runs=total_runs,
            transitions=transitions,
            same_commit_both_passed_and_failed=same_commit,
            regression_detected=True,
            regression_commit=regression_commit,
            flaky_score=flaky_score,
            is_flaky=False,
            status="regression",
            confidence=0.90,
            history=ordered,
            details=(
                f"Genuine regression detected after commit {regression_commit or 'unknown'}; "
                "failed continuously from that point onwards."
            ),
        )

    flaky_score = round(max(0.0, flaky_score), 4)
    is_flaky = flaky_score > FLAKY_SCORE_THRESHOLD
    return FlakinessReport(
        test_name=test_name,
        job=job,
        pass_count=pass_count,
        fail_count=fail_count,
        total_runs=total_runs,
        transitions=transitions,
        same_commit_both_passed_and_failed=same_commit,
        regression_detected=False,
        regression_commit=None,
        flaky_score=flaky_score,
        is_flaky=is_flaky,
        status="flaky" if is_flaky else "not_flaky",
        confidence=min(0.95, round(0.55 + flaky_score * 0.40, 4)),
        history=ordered,
        details=(
            f"Test demonstrated {transitions} transitions across {total_runs} builds "
            f"(same commit inconsistency: {same_commit})."
        ),
    )


def _main_job_fallback(job: JobPath) -> JobPath | None:
    """If this is a branch job with little history, also inspect main/master."""
    if isinstance(job, str) and "/" in job:
        base_job, branch = job.split("/", 1)
        if branch not in {"main", "master"}:
            return f"{base_job}/main"
        return None
    if isinstance(job, (list, tuple)) and len(job) >= 2:
        branch = job[-1]
        if branch not in {"main", "master"}:
            return [*list(job)[:-1], "main"]
    return None


async def analyze_test_flakiness(
    job: JobPath,
    test_name: str,
    client: JenkinsClient,
    max_builds: int = DEFAULT_HISTORY_LIMIT,
) -> FlakinessReport:
    """Inspect the last N builds of job for test_name and compute flakiness report."""
    job_str = job if isinstance(job, str) else "/".join(job)

    tagged_builds: list[tuple[JobPath, dict[str, Any]]] = []
    try:
        builds = await client.get_build_history(job, limit=max_builds)
        tagged_builds.extend((job, build) for build in builds)
    except Exception as exc:
        logger.warning("Failed to fetch build history for %s: %s", job_str, exc)

    if len(tagged_builds) < MIN_BUILDS_FOR_HISTORY:
        main_job = _main_job_fallback(job)
        if main_job is not None:
            try:
                main_builds = await client.get_build_history(main_job, limit=max_builds)
                seen_numbers = {b.get("number") for _, b in tagged_builds}
                for build in main_builds:
                    if build.get("number") not in seen_numbers:
                        tagged_builds.append((main_job, build))
            except Exception as exc:
                logger.debug("Fallback to main returned no history: %s", exc)

    if not tagged_builds:
        return score_from_records(test_name, job_str, [])

    records: list[BuildTestRecord] = []
    for source_job, build in tagged_builds:
        b_num = build.get("number")
        if b_num is None:
            continue

        commit = _extract_commit_from_build(build)
        if not commit:
            try:
                full_build = await client.get_build(source_job, b_num)
                commit = _extract_commit_from_build(full_build)
            except Exception:
                pass

        try:
            report = await client.get_test_report(source_job, b_num)
        except Exception:
            report = None

        if report is None:
            continue

        cases = _extract_cases_from_report(report)
        matched_case = next(
            (
                case
                for case in cases
                if _matches_test(
                    str(case.get("name", "")),
                    str(case.get("className", "")),
                    test_name,
                )
            ),
            None,
        )
        if matched_case is None:
            continue

        status_str = str(matched_case.get("status", "")).upper()
        if status_str == "SKIPPED":
            continue

        passed = status_str in {"PASSED", "FIXED"}
        duration_ms = matched_case.get("duration")
        if duration_ms is not None:
            duration_ms = float(duration_ms) * 1000.0 if float(duration_ms) < 100.0 else float(duration_ms)
        error_msg = matched_case.get("errorDetails") or matched_case.get("message")

        records.append(
            BuildTestRecord(
                build_number=int(b_num),
                passed=passed,
                commit=commit,
                duration_ms=duration_ms,
                error_message=error_msg,
            )
        )

    return score_from_records(test_name, job_str, records)


class CheckFlakinessArgs(BaseModel):
    test_name: str = Field(description="Name of the failing test to check for historical flakiness")


class CheckFlakinessTool(BaseTool):
    """CrewAI tool for checking test flakiness across recent Jenkins builds."""

    name: str = "check_flakiness"
    description: str = (
        "Check whether a failing test is historically flaky across the last 20 Jenkins builds. "
        "Provide test_name as argument. Returns pass/fail counts, transitions, same-commit inconsistency, "
        "and flaky_score in [0, 1]. If flaky, recommend retry / quarantine and investigate; "
        "never recommend changing business logic."
    )
    args_schema: type[BaseModel] = CheckFlakinessArgs
    job: str = ""
    client: Any = Field(default=None, exclude=True)
    max_builds: int = DEFAULT_HISTORY_LIMIT

    def _run(self, test_name: str) -> str:
        if self.client is None:
            return json.dumps({
                "test_name": test_name,
                "error": "JenkinsClient not configured for flakiness tool",
                "flaky_score": 0.0,
                "is_flaky": False,
            })

        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    report = executor.submit(
                        asyncio.run,
                        analyze_test_flakiness(self.job, test_name, self.client, self.max_builds),
                    ).result(timeout=15.0)
            else:
                report = asyncio.run(
                    analyze_test_flakiness(self.job, test_name, self.client, self.max_builds)
                )

            return json.dumps(report.to_dict())
        except Exception as error:
            logger.warning("check_flakiness tool failed: %s", error)
            return json.dumps({
                "test_name": test_name,
                "error": str(error),
                "flaky_score": 0.0,
                "is_flaky": False,
            })


def create_flakiness_tool(job: JobPath, client: JenkinsClient) -> CheckFlakinessTool:
    """Create a configured CheckFlakinessTool instance for an agent."""
    job_str = job if isinstance(job, str) else "/".join(job)
    return CheckFlakinessTool(job=job_str, client=client)
