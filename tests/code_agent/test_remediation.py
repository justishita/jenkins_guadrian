"""Tests for the remediation engine (Week 5).

**Owner:** P3. A fix generator is only safe if it is as good at declining as it is at
fixing, so roughly half of these tests check that no plan comes back. The rest check
that a plan, when it does come back, is exactly the inverse of the breaking lines and
nothing more - and that the real OPA policy accepts it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import UUID

import pytest

from agents.code_agent.remediation import (
	EditNotApplicable,
	FileEdit,
	FixPlan,
	NoFix,
	branch_name_for,
	propose_fix,
)
from agents.code_agent.tools.github_client import FileChange
from common.models import FailureTaxonomy
from coordinator.synthesis import IncidentReport


INCIDENT = UUID("11111111-2222-3333-4444-555555555555")
BASE = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
REPO_ROOT = Path(__file__).resolve().parents[2]

DEP = FailureTaxonomy.DEPENDENCY_REGRESSION
CFG = FailureTaxonomy.CONFIG_ERROR


def report(
	failure_type: FailureTaxonomy = DEP,
	confidence: float = 0.9,
	status: str = "resolved_cause",
) -> IncidentReport:
	return IncidentReport(
		incident_id=INCIDENT,
		created_at=datetime(2026, 10, 25, tzinfo=timezone.utc),
		status=status,  # type: ignore[arg-type]
		failure_type=failure_type,
		confidence=confidence,
		summary=f"{failure_type.value} at {confidence:.2f} confidence.",
	)


def change(path: str, patch: str, *, status: str = "modified") -> FileChange:
	return FileChange(path=path, status=status, additions=1, deletions=1, patch=patch)


TC03 = change("target_app/requirements.txt", "@@ -4,1 +4,1 @@\n-httpx==0.28.1\n+httpx==0.99.0")
TC08 = change(
	"target_app/config/settings.yaml",
	"@@ -2,1 +2,1 @@\n-database_timeout_seconds: 3\n+database_timeout_seconds: -5",
)


# --- TC-03 --------------------------------------------------------------------


def test_a_moved_pin_is_moved_back_exactly() -> None:
	plan = propose_fix(report(DEP), [TC03], base_ref=BASE)

	assert isinstance(plan, FixPlan)
	assert len(plan.edits) == 1
	edit = plan.edits[0]
	assert edit.path == "target_app/requirements.txt"
	assert edit.before == "httpx==0.99.0"
	assert edit.after == "httpx==0.28.1"
	assert "httpx" in plan.commit_message


def test_only_the_pin_that_moved_is_touched() -> None:
	"""A manifest diff that also reorders unchanged pins must not drag them in."""
	patch = "@@ -1,3 +1,3 @@\n-fastapi==0.115.12\n-httpx==0.28.1\n+httpx==0.99.0\n+fastapi==0.115.12"
	plan = propose_fix(report(DEP), [change("target_app/requirements.txt", patch)], base_ref=BASE)

	assert isinstance(plan, FixPlan)
	assert [edit.after for edit in plan.edits] == ["httpx==0.28.1"]


def test_an_added_dependency_has_nothing_to_restore() -> None:
	added = change("target_app/requirements.txt", "@@ -0,0 +1,1 @@\n+requests==2.32.0")

	result = propose_fix(report(DEP), [added], base_ref=BASE)

	assert isinstance(result, NoFix)
	assert "no previous version" in result.reason


# --- TC-08 --------------------------------------------------------------------


def test_a_broken_config_value_is_restored_exactly() -> None:
	plan = propose_fix(report(CFG), [TC08], base_ref=BASE)

	assert isinstance(plan, FixPlan)
	edit = plan.edits[0]
	assert edit.before == "database_timeout_seconds: -5"
	assert edit.after == "database_timeout_seconds: 3"
	assert "not a positive value" in edit.reason


def test_a_plausible_config_change_is_not_reverted() -> None:
	"""Reverting a legitimate change is its own incident."""
	fine = change(
		"target_app/config/settings.yaml",
		"@@ -2,1 +2,1 @@\n-database_timeout_seconds: 3\n+database_timeout_seconds: 10",
	)

	result = propose_fix(report(CFG), [fine], base_ref=BASE)

	assert isinstance(result, NoFix)
	assert "checkably wrong" in result.reason


def test_only_the_broken_key_is_restored_when_several_changed() -> None:
	both = change(
		"target_app/config/settings.yaml",
		"@@ -1,2 +1,2 @@\n-database_timeout_seconds: 3\n-cpu_max_seconds: 5\n"
		"+database_timeout_seconds: -5\n+cpu_max_seconds: 8",
	)

	plan = propose_fix(report(CFG), [both], base_ref=BASE)

	assert isinstance(plan, FixPlan)
	assert [edit.after for edit in plan.edits] == ["database_timeout_seconds: 3"]


def test_an_env_file_value_is_restored_in_its_own_syntax() -> None:
	env = change(
		"target_app/config/deploy.env",
		"@@ -1,1 +1,1 @@\n-ORDERS_PORT=8010\n+ORDERS_PORT=0",
	)

	plan = propose_fix(report(CFG), [env], base_ref=BASE)

	assert isinstance(plan, FixPlan)
	assert plan.edits[0].after == "ORDERS_PORT=8010"


# --- refusing -----------------------------------------------------------------


@pytest.mark.parametrize(
	("status", "confidence"),
	[("conflicting", 0.9), ("insufficient_evidence", 0.3), ("resolved_cause", 0.45)],
)
def test_no_fix_from_an_unactionable_report(status: str, confidence: float) -> None:
	result = propose_fix(report(DEP, confidence, status), [TC03], base_ref=BASE)

	assert isinstance(result, NoFix)
	assert "not actionable" in result.reason


@pytest.mark.parametrize(
	"failure_type",
	[FailureTaxonomy.CODE_TEST_FAILURE, FailureTaxonomy.TIMEOUT, FailureTaxonomy.RESOURCE_EXHAUSTION],
)
def test_no_automated_fix_for_unsupported_failure_types(failure_type: FailureTaxonomy) -> None:
	result = propose_fix(report(failure_type), [TC03], base_ref=BASE)

	assert isinstance(result, NoFix)
	assert "no automated fix" in result.reason


def test_a_dependency_report_never_edits_config_files() -> None:
	"""The fix follows the agreed cause, not whatever else happened to change."""
	plan = propose_fix(report(DEP), [TC03, TC08], base_ref=BASE)

	assert isinstance(plan, FixPlan)
	assert plan.changed_paths == ("target_app/requirements.txt",)


def test_no_fix_from_an_empty_change_set() -> None:
	assert isinstance(propose_fix(report(DEP), [], base_ref=BASE), NoFix)


# --- applying an edit ---------------------------------------------------------


def test_an_edit_replaces_exactly_one_line() -> None:
	edit = FileEdit(path="r.txt", before="httpx==0.99.0", after="httpx==0.28.1")

	assert edit.apply("fastapi==1\nhttpx==0.99.0\nuvicorn==2\n") == "fastapi==1\nhttpx==0.28.1\nuvicorn==2\n"


def test_an_edit_preserves_windows_line_endings() -> None:
	edit = FileEdit(path="r.txt", before="a: -5", after="a: 3")

	assert edit.apply("x: 1\r\na: -5\r\n") == "x: 1\r\na: 3\r\n"


def test_an_edit_preserves_a_missing_final_newline() -> None:
	edit = FileEdit(path="r.txt", before="a: -5", after="a: 3")

	assert edit.apply("x: 1\na: -5") == "x: 1\na: 3"


def test_an_edit_against_a_file_that_moved_on_is_refused() -> None:
	edit = FileEdit(path="r.txt", before="httpx==0.99.0", after="httpx==0.28.1")

	with pytest.raises(EditNotApplicable, match="found 0"):
		edit.apply("httpx==0.30.0\n")


def test_an_ambiguous_edit_is_refused() -> None:
	edit = FileEdit(path="r.txt", before="debug: false", after="debug: true")

	with pytest.raises(EditNotApplicable, match="found 2"):
		edit.apply("debug: false\nother: 1\ndebug: false\n")


def test_a_substring_is_not_a_match() -> None:
	"""`httpx==0.99.0` must not match inside `my-httpx==0.99.0`."""
	edit = FileEdit(path="r.txt", before="httpx==0.99.0", after="httpx==0.28.1")

	with pytest.raises(EditNotApplicable):
		edit.apply("my-httpx==0.99.0\n")


# --- plan shape ---------------------------------------------------------------


def test_branch_names_follow_the_ai_fix_convention() -> None:
	assert branch_name_for(INCIDENT, 0) == "ai-fix/11111111-2222-3333-4444-555555555555-0"
	assert branch_name_for(INCIDENT, 3) == "ai-fix/11111111-2222-3333-4444-555555555555-3"


def test_a_generated_plan_uses_its_own_incident_and_attempt() -> None:
	plan = propose_fix(report(DEP), [TC03], base_ref=BASE, attempt=2)
	assert isinstance(plan, FixPlan)

	assert plan.branch_name == f"ai-fix/{INCIDENT}-2"


def test_a_plan_with_an_unlinkable_branch_is_refused() -> None:
	plan = propose_fix(report(DEP), [TC03], base_ref=BASE)
	assert isinstance(plan, FixPlan)

	for bad in ("remediation/x", f"ai-fix/{INCIDENT}-1", "ai-fix/11111111-0000-0000-0000-000000000000-0"):
		with pytest.raises(ValueError, match="branch_name must be"):
			FixPlan.model_validate({**plan.model_dump(), "branch_name": bad})


def test_the_policy_input_matches_what_the_rego_reads() -> None:
	plan = propose_fix(report(DEP), [TC03], base_ref=BASE)
	assert isinstance(plan, FixPlan)

	document = plan.to_policy_input()

	assert document["confidence"] == pytest.approx(0.9)
	files = document["changed_files"]
	assert isinstance(files, list)
	assert files[0]["path"] == "target_app/requirements.txt"
	assert files[0]["additions"] == 1 and files[0]["deletions"] == 1
	assert "+httpx==0.28.1" in files[0]["patch"]


def test_the_plan_renders_for_a_reviewer() -> None:
	plan = propose_fix(report(CFG), [TC08], base_ref=BASE)
	assert isinstance(plan, FixPlan)

	markdown = plan.to_markdown()

	assert "-database_timeout_seconds: -5" in markdown
	assert "+database_timeout_seconds: 3" in markdown
	assert "Jenkins re-runs" in markdown


def test_the_plan_is_valid_structured_output() -> None:
	plan = propose_fix(report(DEP), [TC03], base_ref=BASE)
	assert isinstance(plan, FixPlan)

	assert FixPlan.model_validate_json(plan.model_dump_json()) == plan


# --- against the real policy --------------------------------------------------


def _opa() -> str | None:
	return os.environ.get("OPA_BIN") or shutil.which("opa")


def _evaluate(document: dict[str, object]) -> dict[str, object]:
	binary = _opa()
	assert binary is not None
	completed = subprocess.run(
		[binary, "eval", "--format", "json", "--stdin-input", "-d", str(REPO_ROOT / "policies"),
		 "data.jenkinsguardians.remediation"],
		input=json.dumps(document),
		capture_output=True,
		text=True,
		timeout=30,
		check=True,
	)
	return json.loads(completed.stdout)["result"][0]["expressions"][0]["value"]


requires_opa = pytest.mark.skipif(_opa() is None, reason="set OPA_BIN or put opa on PATH")


@requires_opa
@pytest.mark.parametrize("failure_type,files", [(DEP, [TC03]), (CFG, [TC08])])
def test_the_real_policy_allows_the_generated_fix(
	failure_type: FailureTaxonomy, files: list[FileChange]
) -> None:
	plan = propose_fix(report(failure_type), files, base_ref=BASE)
	assert isinstance(plan, FixPlan)

	verdict = _evaluate(plan.to_policy_input())

	assert verdict["allow"] is True, verdict.get("deny")


@requires_opa
def test_the_real_policy_blocks_a_fix_that_skips_a_test() -> None:
	"""TC-15: the obvious 'fix' that hides the symptom must never get through."""
	unsafe = FixPlan(
		incident_id=INCIDENT,
		failure_type=FailureTaxonomy.CODE_TEST_FAILURE,
		confidence=0.9,
		base_ref=BASE,
		branch_name="ai-fix/11111111-2222-3333-4444-555555555555-0",
		commit_message="skip it",
		rationale="make it green",
		edits=(
			FileEdit(
				path="target_app/tests/test_orders.py",
				before="def test_orders():",
				after="@pytest.mark.skip(reason='flaky')",
			),
		),
	)

	verdict = _evaluate(unsafe.to_policy_input())

	assert verdict["allow"] is False
	assert any("disables a test" in message for message in verdict["deny"])
