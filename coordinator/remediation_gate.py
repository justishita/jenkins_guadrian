"""Week 6: the gated path from a proposed fix to a draft pull request.

**Owner:** P3. Nothing reaches GitHub except through here, and here has three gates
that must all pass, in order:

1. **Remediation policy** (``policies/remediation.rego``). Is this fix safe and
   minimal enough to show a human? Blocks unsafe fixes such as skipping a test
   (TC-15). Runs before any human is asked, so nobody's attention is spent on a
   proposal that was never allowed to exist.
2. **A recorded human decision.** Approve, reject, or ask for changes, with a named
   reviewer. A rejection ends the proposal and nothing is merged or deployed (TC-16).
3. **Pull-request policy** (``policies/pull_request.rego``). Re-checks that the
   proposal passed policy, that a named human approved it, that the PR is a draft
   and that auto-merge is off. This gate cannot be satisfied by the machine alone.

Only then is a branch created and a **draft** pull request opened (TC-11). Jenkins
then re-runs the pipeline on that branch; whether the fix holds is decided there.

The workflow is split into ``submit`` and ``decide`` because approval is asynchronous:
a human answers when they answer. Every step is written to the audit trail, including
every refusal - a blocked proposal is the most interesting thing to have recorded.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agents.code_agent.remediation import FixPlan, apply_plan_to_branch
from agents.code_agent.tools.github_writer import GitHubWriter, GitHubWriteError
from common.audit import AuditLog, AuditRecord
from coordinator.policy import PolicyError, PolicyVerdict


logger = logging.getLogger(__name__)

ACTOR = "coordinator"

Status = Literal[
    "policy_rejected",
    "awaiting_approval",
    "rejected",
    "pr_blocked",
    "pr_created",
    "failed",
]
Decision = Literal["approved", "rejected", "changes_requested"]


class PolicyEvaluator(Protocol):
    """Anything that can answer the two policy questions: the HTTP or the CLI gate."""

    async def evaluate_remediation(self, proposal: dict[str, Any]) -> PolicyVerdict: ...

    async def evaluate_pull_request(self, request: dict[str, Any]) -> PolicyVerdict: ...


class WorkflowError(RuntimeError):
    """A step was attempted out of order, e.g. approving a rejected proposal."""


class Approval(BaseModel):
    """A human's decision on one proposal."""

    model_config = ConfigDict(frozen=True)

    decision: Decision
    reviewer: str = Field(min_length=1)
    reason: str = ""
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RemediationRecord(BaseModel):
    """Where one proposal stands, and every reason it stands there."""

    model_config = ConfigDict(frozen=True)

    incident_id: UUID
    branch_name: str
    attempt: int
    status: Status
    policy_allowed: bool = False
    violations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    approval: Approval | None = None
    pull_request_number: int | None = None
    pull_request_url: str | None = None
    detail: str = ""

    @property
    def awaiting_human(self) -> bool:
        return self.status == "awaiting_approval"


class RemediationGate:
    """Drives a FixPlan through policy, approval and PR policy to a draft PR."""

    def __init__(
        self,
        policy: PolicyEvaluator,
        writer: GitHubWriter,
        audit: AuditLog,
        *,
        base_branch: str = "main",
    ) -> None:
        self._policy = policy
        self._writer = writer
        self._audit = audit
        self._base_branch = base_branch

    async def _record(
        self, plan: FixPlan, event_type: str, summary: str, payload: dict[str, Any], *, ok: bool
    ) -> None:
        await self._audit.record(
            AuditRecord(
                incident_id=plan.incident_id,
                actor=ACTOR,
                event_type=event_type,
                summary=summary,
                payload={"branch": plan.branch_name, "attempt": plan.attempt, **payload},
                ok=ok,
            )
        )

    @staticmethod
    def _record_for(plan: FixPlan, status: Status, **fields: Any) -> RemediationRecord:
        return RemediationRecord(
            incident_id=plan.incident_id,
            branch_name=plan.branch_name,
            attempt=plan.attempt,
            status=status,
            **fields,
        )

    # --- gate 1 ---------------------------------------------------------------

    async def submit(self, plan: FixPlan) -> RemediationRecord:
        """Check a proposal against the remediation policy before any human sees it.

        Fails closed: if the policy engine cannot answer, the proposal is rejected.
        """
        await self._record(
            plan,
            "remediation_proposed",
            plan.commit_message.splitlines()[0],
            {
                "failure_type": plan.failure_type.value,
                "confidence": plan.confidence,
                "changed_files": list(plan.changed_paths),
                "edits": len(plan.edits),
            },
            ok=True,
        )

        try:
            verdict = await self._policy.evaluate_remediation(plan.to_policy_input())
        except PolicyError as error:
            await self._record(
                plan,
                "policy_evaluated",
                "policy engine could not answer; proposal blocked",
                {"allowed": False, "error": str(error)},
                ok=False,
            )
            return self._record_for(
                plan,
                "policy_rejected",
                violations=("policy engine unavailable; blocked by default",),
                detail=str(error),
            )

        await self._record(
            plan,
            "policy_evaluated",
            verdict.reason(),
            {
                "package": verdict.package,
                "allowed": verdict.allowed,
                "violations": list(verdict.violations),
                "warnings": list(verdict.warnings),
            },
            ok=verdict.allowed,
        )
        if not verdict.allowed:
            return self._record_for(
                plan,
                "policy_rejected",
                violations=verdict.violations,
                warnings=verdict.warnings,
                detail="Blocked by the remediation policy; a human must handle this incident.",
            )

        await self._record(
            plan,
            "approval_requested",
            "proposal passed policy; waiting for a reviewer",
            {"warnings": list(verdict.warnings)},
            ok=True,
        )
        return self._record_for(
            plan,
            "awaiting_approval",
            policy_allowed=True,
            warnings=verdict.warnings,
        )

    # --- gates 2 and 3, then the draft PR ---------------------------------------

    async def decide(
        self, plan: FixPlan, record: RemediationRecord, approval: Approval
    ) -> RemediationRecord:
        """Apply a human decision; on approval, pass the PR policy and open a draft PR."""
        if record.incident_id != plan.incident_id or record.branch_name != plan.branch_name:
            raise WorkflowError("this record does not belong to this plan")
        if not record.awaiting_human:
            raise WorkflowError(
                f"proposal is {record.status}; only a proposal awaiting approval can be decided"
            )

        await self._record(
            plan,
            "approval_decided",
            f"{approval.reviewer} {approval.decision} the proposal",
            {
                "decision": approval.decision,
                "reviewer": approval.reviewer,
                "reason": approval.reason,
            },
            ok=approval.decision == "approved",
        )
        if approval.decision != "approved":
            # TC-16: no branch, no PR, no deployment. The decision is the record.
            return record.model_copy(
                update={
                    "status": "rejected",
                    "approval": approval,
                    "detail": f"{approval.decision} by {approval.reviewer}"
                    + (f": {approval.reason}" if approval.reason else ""),
                }
            )

        request = {
            "policy_allowed": record.policy_allowed,
            "draft": True,
            "auto_merge": False,
            "approval": {"decision": approval.decision, "reviewer": approval.reviewer},
        }
        try:
            verdict = await self._policy.evaluate_pull_request(request)
        except PolicyError as error:
            verdict = None
            failure = str(error)
        else:
            failure = ""

        if verdict is None or not verdict.allowed:
            # `is not None`, never truthiness: a denying verdict is falsy, and testing it
            # that way would replace the real violations with "engine unavailable".
            violations = (
                verdict.violations
                if verdict is not None
                else ("policy engine unavailable; blocked by default",)
            )
            await self._record(
                plan,
                "policy_evaluated",
                "pull request blocked by policy",
                {"package": "jenkinsguardians/pullrequest", "allowed": False, "violations": list(violations), "error": failure},
                ok=False,
            )
            return record.model_copy(
                update={"status": "pr_blocked", "approval": approval, "violations": violations, "detail": failure}
            )

        try:
            branch = await apply_plan_to_branch(plan, self._writer)
            pull_request = await self._writer.open_draft_pull_request(
                head=branch.branch,
                base=self._base_branch,
                title=plan.commit_message.splitlines()[0],
                body=(
                    f"{plan.to_markdown()}\n\n---\n"
                    f"Approved by **{approval.reviewer}**"
                    f"{f': {approval.reason}' if approval.reason else ''}. "
                    "Opened as a draft; Jenkins re-validates it before anyone merges."
                ),
            )
        except (GitHubWriteError, ValueError) as error:
            # ValueError covers EditNotApplicable: the fix went stale before it landed.
            await self._record(
                plan,
                "pull_request_created",
                f"could not open the pull request: {error}",
                {"error": str(error)},
                ok=False,
            )
            return record.model_copy(
                update={"status": "failed", "approval": approval, "detail": str(error)}
            )

        await self._record(
            plan,
            "pull_request_created",
            f"draft pull request #{pull_request.number} opened",
            {
                "number": pull_request.number,
                "url": pull_request.url,
                "draft": pull_request.draft,
                "commits": list(branch.commits),
                "reviewer": approval.reviewer,
            },
            ok=True,
        )
        return record.model_copy(
            update={
                "status": "pr_created",
                "approval": approval,
                "pull_request_number": pull_request.number,
                "pull_request_url": pull_request.url,
                "detail": "Draft opened; awaiting Jenkins re-validation.",
            }
        )


__all__ = [
    "Approval",
    "PolicyEvaluator",
    "RemediationGate",
    "RemediationRecord",
    "WorkflowError",
]
