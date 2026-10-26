"""Tests for the gated remediation workflow (Week 6).

**Owner:** P3. The workflow's job is mostly to say no, so most of these tests check
that nothing reached GitHub: an unsafe fix (TC-15), a rejected fix (TC-16), a policy
engine that cannot answer, a step taken out of order. The happy path (TC-11) is the
minority case, and it ends in a *draft*.

Two policy back-ends are used. ``FakePolicy`` makes the gate logic deterministic; the
``requires_opa`` tests run the same proposals through the real Rego via ``OpaCliGate``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import UUID

import httpx
import pytest

from agents.code_agent.remediation import FileEdit, FixPlan, propose_fix
from agents.code_agent.tools.github_client import FileChange
from agents.code_agent.tools.github_writer import GitHubWriter
from common.audit import FileAuditLog
from common.models import Evidence, FailureTaxonomy
from coordinator.policy import (
	PULL_REQUEST_PACKAGE,
	REMEDIATION_PACKAGE,
	OpaCliGate,
	PolicyUnavailable,
	PolicyVerdict,
)
from coordinator.remediation_gate import Approval, RemediationGate, WorkflowError
from coordinator.synthesis import synthesize
from tests.code_agent.test_github_writer import FakeGitHub


INCIDENT = UUID("11111111-2222-3333-4444-555555555555")
BASE = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ENV = "ORDERS_PORT=0\nDATABASE_URL=\nORDERS_ENABLE_SLOW=false\n"


class FakePolicy:
	"""Answers both policy questions from fixed verdicts, recording what it was asked."""

	def __init__(
		self,
		remediation: PolicyVerdict | Exception | None = None,
		pull_request: PolicyVerdict | Exception | None = None,
	) -> None:
		# `is None`, not `or`: a denying PolicyVerdict is falsy (its __bool__ is
		# `allowed`), so `denied or default` would quietly swap in the allow.
		self._remediation = (
			PolicyVerdict(REMEDIATION_PACKAGE, True) if remediation is None else remediation
		)
		self._pull_request = (
			PolicyVerdict(PULL_REQUEST_PACKAGE, True) if pull_request is None else pull_request
		)
		self.asked: list[tuple[str, dict[str, Any]]] = []

	async def evaluate_remediation(self, proposal: dict[str, Any]) -> PolicyVerdict:
		self.asked.append(("remediation", proposal))
		if isinstance(self._remediation, Exception):
			raise self._remediation
		return self._remediation

	async def evaluate_pull_request(self, request: dict[str, Any]) -> PolicyVerdict:
		self.asked.append(("pull_request", request))
		if isinstance(self._pull_request, Exception):
			raise self._pull_request
		return self._pull_request


def tc11_plan() -> FixPlan:
	"""TC-11: an invalid deployment config value, fused into a config_error report."""
	evidence = [
		Evidence(
			incident_id=INCIDENT,
			agent=agent,  # type: ignore[arg-type]
			created_at=datetime(2026, 10, 25, tzinfo=timezone.utc),
			status="completed",
			failure_type=FailureTaxonomy.CONFIG_ERROR,
			summary=f"{agent}: invalid deploy configuration",
			confidence=confidence,
			redaction_applied=True,
		)
		for agent, confidence in (("code_agent", 0.9), ("jenkins_agent", 0.85))
	]
	# These tests are about the gate, not completeness: the two agents here are the whole investigation.
	report = synthesize(evidence, expected_agents=[item.agent for item in evidence])
	files = [
		FileChange(
			path="target_app/config/deploy.env",
			status="modified",
			additions=1,
			deletions=1,
			patch="@@ -1,1 +1,1 @@\n-ORDERS_PORT=8010\n+ORDERS_PORT=0",
		)
	]
	plan = propose_fix(report, files, base_ref=BASE)
	assert isinstance(plan, FixPlan)
	return plan


def unsafe_plan() -> FixPlan:
	"""TC-15: the 'fix' that makes the symptom disappear by skipping the test."""
	return FixPlan(
		incident_id=INCIDENT,
		failure_type=FailureTaxonomy.CODE_TEST_FAILURE,
		confidence=0.9,
		base_ref=BASE,
		branch_name="ai-fix/11111111-2222-3333-4444-555555555555-0",
		commit_message="Make the build green",
		rationale="Skip the failing test.",
		edits=(
			FileEdit(
				path="target_app/tests/test_orders.py",
				before="def test_orders():",
				after="@pytest.mark.skip(reason='flaky')",
			),
		),
	)


@pytest.fixture
def github() -> FakeGitHub:
	return FakeGitHub({"target_app/config/deploy.env": DEPLOY_ENV})


@pytest.fixture
def audit(tmp_path: Path) -> FileAuditLog:
	return FileAuditLog(tmp_path / "audit")


def gate_for(policy: Any, github: FakeGitHub, audit: FileAuditLog) -> RemediationGate:
	writer = GitHubWriter("o/r", "t", client=httpx.AsyncClient(transport=httpx.MockTransport(github.handler)))
	return RemediationGate(policy, writer, audit)


APPROVE = Approval(decision="approved", reviewer="ishita", reason="Port 0 is clearly wrong.")


# --- TC-11: the happy path ends in a draft -------------------------------------


@pytest.mark.asyncio
async def test_tc11_an_approved_fix_becomes_a_draft_pull_request(
	github: FakeGitHub, audit: FileAuditLog
) -> None:
	plan = tc11_plan()
	gate = gate_for(FakePolicy(), github, audit)

	submitted = await gate.submit(plan)
	assert submitted.status == "awaiting_approval"
	assert github.branches == {}  # nothing touches GitHub before a human decides

	final = await gate.decide(plan, submitted, APPROVE)

	assert final.status == "pr_created"
	assert final.pull_request_number == 42
	assert github.prs[0]["draft"] is True
	assert github.prs[0]["base"] == "main"
	assert github.files["target_app/config/deploy.env"].startswith("ORDERS_PORT=8010\n")


@pytest.mark.asyncio
async def test_the_pull_request_names_the_reviewer_and_shows_the_diff(
	github: FakeGitHub, audit: FileAuditLog
) -> None:
	plan = tc11_plan()
	gate = gate_for(FakePolicy(), github, audit)

	await gate.decide(plan, await gate.submit(plan), APPROVE)
	body = github.prs[0]["body"]

	assert "Approved by **ishita**" in body
	assert "-ORDERS_PORT=0" in body and "+ORDERS_PORT=8010" in body
	assert "Jenkins re-validates" in body


@pytest.mark.asyncio
async def test_the_pr_policy_is_asked_with_a_draft_and_no_auto_merge(
	github: FakeGitHub, audit: FileAuditLog
) -> None:
	plan = tc11_plan()
	policy = FakePolicy()
	gate = gate_for(policy, github, audit)

	await gate.decide(plan, await gate.submit(plan), APPROVE)
	_, request = policy.asked[-1]

	assert request == {
		"policy_allowed": True,
		"draft": True,
		"auto_merge": False,
		"approval": {"decision": "approved", "reviewer": "ishita"},
	}


# --- TC-15: an unsafe fix never reaches a human ----------------------------------


@pytest.mark.asyncio
async def test_tc15_a_blocked_fix_is_rejected_before_anyone_is_asked(
	github: FakeGitHub, audit: FileAuditLog
) -> None:
	blocked = PolicyVerdict(REMEDIATION_PACKAGE, False, violations=("proposal disables a test",))
	gate = gate_for(FakePolicy(remediation=blocked), github, audit)

	record = await gate.submit(unsafe_plan())

	assert record.status == "policy_rejected"
	assert record.violations == ("proposal disables a test",)
	assert github.calls == []


@pytest.mark.asyncio
async def test_a_blocked_fix_cannot_then_be_approved(github: FakeGitHub, audit: FileAuditLog) -> None:
	blocked = PolicyVerdict(REMEDIATION_PACKAGE, False, violations=("no",))
	gate = gate_for(FakePolicy(remediation=blocked), github, audit)
	plan = unsafe_plan()
	record = await gate.submit(plan)

	with pytest.raises(WorkflowError, match="policy_rejected"):
		await gate.decide(plan, record, APPROVE)

	assert github.calls == []


# --- TC-16: a human rejection ends it --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["rejected", "changes_requested"])
async def test_tc16_a_rejection_creates_no_branch_and_no_pull_request(
	decision: str, github: FakeGitHub, audit: FileAuditLog
) -> None:
	plan = tc11_plan()
	gate = gate_for(FakePolicy(), github, audit)

	final = await gate.decide(
		plan, await gate.submit(plan), Approval(decision=decision, reviewer="shreya", reason="Wrong port.")  # type: ignore[arg-type]
	)

	assert final.status == "rejected"
	assert final.detail == f"{decision} by shreya: Wrong port."
	assert github.calls == []
	assert github.prs == []


@pytest.mark.asyncio
async def test_the_rejection_is_recorded_in_the_audit_trail(github: FakeGitHub, audit: FileAuditLog) -> None:
	plan = tc11_plan()
	gate = gate_for(FakePolicy(), github, audit)

	await gate.decide(plan, await gate.submit(plan), Approval(decision="rejected", reviewer="shreya"))
	decided = [r for r in await audit.read(INCIDENT) if r.event_type == "approval_decided"]

	assert len(decided) == 1
	assert decided[0].payload["reviewer"] == "shreya"
	assert decided[0].ok is False


# --- failing closed ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreachable_policy_engine_blocks_the_proposal(
	github: FakeGitHub, audit: FileAuditLog
) -> None:
	gate = gate_for(FakePolicy(remediation=PolicyUnavailable("OPA down")), github, audit)

	record = await gate.submit(tc11_plan())

	assert record.status == "policy_rejected"
	assert "unavailable" in record.violations[0]
	assert github.calls == []


@pytest.mark.asyncio
async def test_an_unreachable_policy_engine_at_pr_time_opens_nothing(
	github: FakeGitHub, audit: FileAuditLog
) -> None:
	plan = tc11_plan()
	gate = gate_for(FakePolicy(pull_request=PolicyUnavailable("OPA down")), github, audit)

	final = await gate.decide(plan, await gate.submit(plan), APPROVE)

	assert final.status == "pr_blocked"
	assert github.prs == []
	assert github.branches == {}


@pytest.mark.asyncio
async def test_a_pr_policy_denial_opens_nothing(github: FakeGitHub, audit: FileAuditLog) -> None:
	plan = tc11_plan()
	denied = PolicyVerdict(PULL_REQUEST_PACKAGE, False, violations=("the approval does not name a reviewer",))
	gate = gate_for(FakePolicy(pull_request=denied), github, audit)

	final = await gate.decide(plan, await gate.submit(plan), APPROVE)

	assert final.status == "pr_blocked"
	assert final.violations == ("the approval does not name a reviewer",)
	assert github.prs == []


@pytest.mark.asyncio
async def test_a_fix_that_went_stale_is_recorded_as_failed_not_forced(audit: FileAuditLog) -> None:
	"""Between approval and branching, someone already fixed the file."""
	moved_on = FakeGitHub({"target_app/config/deploy.env": "ORDERS_PORT=9000\n"})
	plan = tc11_plan()
	gate = gate_for(FakePolicy(), moved_on, audit)

	final = await gate.decide(plan, await gate.submit(plan), APPROVE)

	assert final.status == "failed"
	assert "exactly once" in final.detail
	assert moved_on.prs == []


# --- ordering and the trail ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_proposal_cannot_be_decided_twice(github: FakeGitHub, audit: FileAuditLog) -> None:
	plan = tc11_plan()
	gate = gate_for(FakePolicy(), github, audit)
	final = await gate.decide(plan, await gate.submit(plan), APPROVE)

	with pytest.raises(WorkflowError):
		await gate.decide(plan, final, APPROVE)

	assert len(github.prs) == 1


@pytest.mark.asyncio
async def test_a_record_cannot_be_used_with_another_plan(github: FakeGitHub, audit: FileAuditLog) -> None:
	gate = gate_for(FakePolicy(), github, audit)
	record = await gate.submit(tc11_plan())
	other = tc11_plan().model_copy(update={"branch_name": "ai-fix/11111111-2222-3333-4444-555555555555-9"})

	with pytest.raises(WorkflowError, match="does not belong"):
		await gate.decide(other, record, APPROVE)


@pytest.mark.asyncio
async def test_every_step_is_audited_in_order(github: FakeGitHub, audit: FileAuditLog) -> None:
	plan = tc11_plan()
	gate = gate_for(FakePolicy(), github, audit)

	await gate.decide(plan, await gate.submit(plan), APPROVE)
	trail = [r.event_type for r in await audit.read(INCIDENT)]

	assert trail == [
		"remediation_proposed",
		"policy_evaluated",
		"approval_requested",
		"approval_decided",
		"pull_request_created",
	]


# --- the real Rego, through the CLI gate --------------------------------------------


def _opa() -> str | None:
	return os.environ.get("OPA_BIN") or shutil.which("opa")


requires_opa = pytest.mark.skipif(_opa() is None, reason="set OPA_BIN or put opa on PATH")


def real_policy() -> OpaCliGate:
	binary = _opa()
	assert binary is not None
	return OpaCliGate(binary, str(REPO_ROOT / "policies"))


@requires_opa
@pytest.mark.asyncio
async def test_tc11_end_to_end_against_the_real_policy(github: FakeGitHub, audit: FileAuditLog) -> None:
	plan = tc11_plan()
	gate = gate_for(real_policy(), github, audit)

	submitted = await gate.submit(plan)
	assert submitted.status == "awaiting_approval", submitted.violations

	final = await gate.decide(plan, submitted, APPROVE)
	assert final.status == "pr_created", final.violations
	assert github.prs[0]["draft"] is True


@requires_opa
@pytest.mark.asyncio
async def test_tc15_end_to_end_against_the_real_policy(github: FakeGitHub, audit: FileAuditLog) -> None:
	record = await gate_for(real_policy(), github, audit).submit(unsafe_plan())

	assert record.status == "policy_rejected"
	assert any("disables a test" in v for v in record.violations)
	assert github.calls == []


@requires_opa
@pytest.mark.asyncio
async def test_the_real_pr_policy_refuses_an_unnamed_reviewer() -> None:
	verdict = await real_policy().evaluate_pull_request(
		{"policy_allowed": True, "draft": True, "auto_merge": False, "approval": {"decision": "approved", "reviewer": ""}}
	)

	assert verdict.allowed is False


@requires_opa
@pytest.mark.asyncio
async def test_the_cli_gate_refuses_an_undefined_package() -> None:
	from coordinator.policy import PolicyResponseInvalid

	with pytest.raises(PolicyResponseInvalid):
		await real_policy().evaluate("jenkinsguardians/does_not_exist", {})


@pytest.mark.asyncio
async def test_the_cli_gate_fails_closed_without_a_binary() -> None:
	gate = OpaCliGate("/definitely/not/opa", str(REPO_ROOT / "policies"))

	with pytest.raises(PolicyUnavailable):
		await gate.evaluate_remediation({})
