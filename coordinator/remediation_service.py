"""The live remediation loop: from an actionable report to a draft PR, through a human.

**Owner:** P3. ``remediation_gate.py`` holds the three gates; this module connects them
to the running system, which until now never called them:

1. After every report revision, ``on_report`` proposes a minimal fix for an actionable
   incident (``agents/code_agent/remediation.py``), runs gate 1 (the remediation
   policy), keeps the plan, and registers it with the backend. The backend stores it and,
   if policy allowed it, publishes ``approval.requested`` so the notifier asks a reviewer.
2. When the reviewer decides, the backend publishes ``approval.decided``; ``on_decision``
   runs gates 2 and 3 and, on approval, opens the draft PR, then reports the outcome
   back to the backend.

Both steps are idempotent per ``(incident_id, attempt)``: a plan is proposed once, and a
decision is applied once, however often a revision lands or a message is redelivered.
Failures are logged and audited, never raised into the Coordinator's dispatch.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict

from agents.code_agent.remediation import FixPlan, NoFix, propose_fix
from agents.code_agent.tools.github_client import GitHubClient, GitHubError
from common.audit import AuditLog, AuditRecord
from common.models import IncidentCreatedEvent
from coordinator.remediation_gate import Approval, RemediationGate, RemediationRecord
from coordinator.synthesis import IncidentReport


logger = logging.getLogger(__name__)

ACTOR = "coordinator"


class ApprovalBackend(Protocol):
    """Where proposals are registered for review and outcomes reported."""

    async def register(self, payload: dict[str, Any]) -> None: ...

    async def report_outcome(self, incident_id: UUID, attempt: int, payload: dict[str, Any]) -> None: ...


class BackendUnavailable(RuntimeError):
    """The backend could not be reached or kept failing within the retry budget."""


class HttpApprovalBackend:
    """The backend's ``/remediations`` API, with a bounded timeout and retries.

    Retrying a registration is safe because the backend de-duplicates it per incident
    attempt; a 4xx is not retried.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url or not token:
            raise ValueError("BACKEND_URL and APPROVAL_API_TOKEN must be configured")
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._client = client

    async def _send(self, method: str, path: str, payload: dict[str, Any]) -> None:
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            for attempt in range(self._max_retries + 1):
                try:
                    response = await client.request(method, f"{self._base}{path}", json=payload, headers=self._headers)
                except (httpx.TimeoutException, httpx.TransportError) as error:
                    failure: Exception = error
                else:
                    if response.status_code < 500:
                        if response.status_code >= 400:
                            raise BackendUnavailable(f"{method} {path} refused: HTTP {response.status_code}")
                        return
                    failure = BackendUnavailable(f"{method} {path}: HTTP {response.status_code}")
                if attempt == self._max_retries:
                    raise BackendUnavailable(str(failure)) from failure
                await asyncio.sleep(min(self._backoff * (2**attempt), 4.0))
        finally:
            if self._client is None:
                await client.aclose()

    async def register(self, payload: dict[str, Any]) -> None:
        await self._send("POST", "/remediations", payload)

    async def report_outcome(self, incident_id: UUID, attempt: int, payload: dict[str, Any]) -> None:
        await self._send("PATCH", f"/remediations/{incident_id}/{attempt}", payload)


class StoredRemediation(BaseModel):
    """A plan and where it stands, kept so a later decision can be applied to it."""

    model_config = ConfigDict(frozen=True)

    plan: FixPlan
    record: RemediationRecord
    # False until the backend has the proposal; a later report retries the registration.
    registered: bool = False


class PlanStore:
    """``<root>/<incident_id>/a<attempt>.json``, written atomically."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, incident_id: UUID | str, attempt: int) -> Path:
        return self.root / str(UUID(str(incident_id))) / f"a{int(attempt)}.json"

    def load(self, incident_id: UUID | str, attempt: int) -> StoredRemediation | None:
        path = self._path(incident_id, attempt)
        if not path.is_file():
            return None
        return StoredRemediation.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, plan: FixPlan, record: RemediationRecord, *, registered: bool) -> None:
        path = self._path(plan.incident_id, plan.attempt)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = StoredRemediation(plan=plan, record=record, registered=registered).model_dump_json(indent=2)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
            handle.write(document)
        os.replace(handle.name, path)


def _diff(plan: FixPlan) -> str:
    lines: list[str] = []
    for path in plan.changed_paths:
        lines += [f"--- a/{path}", f"+++ b/{path}"]
        for edit in (e for e in plan.edits if e.path == path):
            lines += ["@@", f"-{edit.before}", f"+{edit.after}"]
    return "\n".join(lines) + "\n"


class RemediationService:
    """Proposes fixes for actionable reports and applies human decisions to them."""

    def __init__(
        self,
        gate: RemediationGate,
        reader: Callable[[], GitHubClient],
        backend: ApprovalBackend,
        plans: PlanStore,
        audit: AuditLog,
    ) -> None:
        self._gate = gate
        self._reader = reader
        self._backend = backend
        self._plans = plans
        self._audit = audit
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}

    def _lock(self, incident_id: UUID | str, attempt: int) -> asyncio.Lock:
        return self._locks.setdefault((str(incident_id), attempt), asyncio.Lock())

    async def _note(self, incident_id: UUID, summary: str, payload: dict[str, Any], *, ok: bool) -> None:
        await self._audit.record(
            AuditRecord(
                incident_id=incident_id,
                actor=ACTOR,
                event_type="remediation_proposed",
                summary=summary,
                payload=payload,
                ok=ok,
            )
        )

    # --- step 1: report -> proposal -> policy -> review request --------------------

    async def on_report(self, event: IncidentCreatedEvent, report: IncidentReport) -> RemediationRecord | NoFix | None:
        """Propose a fix for an actionable report, once per incident attempt."""
        if not report.actionable:
            return None
        attempt = event.remediation_attempt
        async with self._lock(event.incident_id, attempt):
            stored = self._plans.load(event.incident_id, attempt)
            if stored is not None:
                if not stored.registered:
                    await self._register(stored.plan, stored.record)
                return stored.record

            try:
                reader = self._reader()
                _, files = await reader.get_commit(event.git_commit)
            except GitHubError as error:
                await self._note(
                    event.incident_id,
                    "could not read the breaking commit; no fix proposed",
                    {"attempt": attempt, "error": str(error)},
                    ok=False,
                )
                return None

            plan = propose_fix(report, files, base_ref=event.git_commit, attempt=attempt)
            if isinstance(plan, NoFix):
                await self._note(event.incident_id, f"No fix proposed: {plan.reason}", {"attempt": attempt}, ok=False)
                return plan

            record = await self._gate.submit(plan)  # gate 1, audited by the gate
            self._plans.save(plan, record, registered=False)
            await self._register(plan, record)
            return record

    async def _register(self, plan: FixPlan, record: RemediationRecord) -> None:
        try:
            await self._backend.register(self._registration(plan, record))
        except BackendUnavailable as error:
            # Kept unregistered: the next report revision for this incident retries it.
            logger.error("Could not register the proposal with the backend (%s)", error)
            return
        self._plans.save(plan, record, registered=True)

    @staticmethod
    def _registration(plan: FixPlan, record: RemediationRecord) -> dict[str, Any]:
        return {
            "incident_id": str(plan.incident_id),
            "attempt": plan.attempt,
            "failure_type": plan.failure_type.value,
            "confidence": plan.confidence,
            "rationale": plan.rationale,
            "diff": _diff(plan),
            "changed_files": list(plan.changed_paths),
            "policy": {
                "package": "jenkinsguardians/remediation",
                "allowed": record.policy_allowed,
                "violations": list(record.violations),
                "warnings": list(record.warnings),
            },
        }

    # --- step 2: decision -> PR policy -> draft PR ---------------------------------

    async def on_decision(self, message: dict[str, Any]) -> RemediationRecord | None:
        """Apply an ``approval.decided`` message to its stored plan, once."""
        try:
            incident_id = UUID(str(message["incident_id"]))
            attempt = int(message.get("attempt", 0))
            approval = Approval(
                decision=message["decision"],
                reviewer=str(message.get("reviewer", "")),
                reason=str(message.get("comment", "")),
            )
        except (KeyError, ValueError, TypeError) as error:
            logger.error("Discarding malformed approval.decided message (%s)", type(error).__name__)
            return None

        async with self._lock(incident_id, attempt):
            stored = self._plans.load(incident_id, attempt)
            if stored is None:
                logger.warning("approval.decided for an incident attempt with no stored plan", extra={"incident_id": str(incident_id)})
                return None
            if not stored.record.awaiting_human:
                return stored.record  # already applied: a redelivered message changes nothing

            final = await self._gate.decide(stored.plan, stored.record, approval)  # gates 2 and 3
            self._plans.save(stored.plan, final, registered=stored.registered)
            if final.status in {"pr_created", "pr_blocked", "failed"}:
                try:
                    await self._backend.report_outcome(
                        incident_id,
                        attempt,
                        {"status": final.status, "pull_request_url": final.pull_request_url, "detail": final.detail},
                    )
                except BackendUnavailable as error:
                    logger.error("Could not report the remediation outcome to the backend (%s)", error)
            return final


__all__ = [
    "ApprovalBackend",
    "BackendUnavailable",
    "HttpApprovalBackend",
    "PlanStore",
    "RemediationService",
    "StoredRemediation",
]
