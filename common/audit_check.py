"""Audit-trail completeness check, per run rather than per incident.

**Owner:** P2 (Week 5). An incident can legitimately have several runs of the same agent
(redelivery, reinvestigation), so the unit checked is the *run*: every ``agent_started`` must
be followed by exactly one terminal event (``agent_completed`` or ``agent_failed``) before that
actor's next start. Report syntheses are checked the same way: each persisted revision has
exactly one audit event keyed by ``report_revision_id``.

**Week 7:** the remediation lifecycle is checked too (request -> policy -> decision -> pull request ->
re-validation -> status). A broken guarantee (a pull request before approval, an approval over a policy
refusal, a status chain that does not add up) is an ``error``. A trail that merely lacks the events the
remediation gate writes is a ``warning`` of kind ``gate_not_in_live_path``: the live pull-request path does
not use the gate yet, so those events are missing there, and failing every live incident for it would hide
the real errors.

``check_incident`` is pure (it takes already-read records), so it runs against the file trail,
the PostgreSQL trail, or a test fixture alike. ``scripts/check_audit.py`` is the CLI.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from common.audit import AuditRecord
from common.redaction import redact

TERMINAL = ("agent_completed", "agent_failed")
SYNTHESIS = ("synthesis_completed", "synthesis_revised")
DISPATCH = "agent_dispatch"
OUTCOMES = ("completed", "failed", "timed_out", "not_dispatched")
TERMINAL_STATUSES = ("RESOLVED", "ESCALATED", "CLOSED_REJECTED")
GATE_WARNING = "gate_not_in_live_path"


@dataclass(frozen=True)
class Problem:
    kind: str  # unfinished_run | orphan_terminal | duplicate_revision | missing_revision_id | ...
    actor: str
    detail: str
    severity: str = "error"  # "error" fails the check; "warning" is reported only

    def __str__(self) -> str:
        tag = "" if self.severity == "error" else f" ({self.severity})"
        return f"[{self.kind}]{tag} {self.actor}: {self.detail}"


def errors(problems: Iterable[Problem]) -> list[Problem]:
    return [problem for problem in problems if problem.severity == "error"]


def check_incident(
    records: Sequence[AuditRecord],
    *,
    required_agents: Iterable[str] = ("jenkins_agent", "metrics_agent", "code_agent"),
    unaudited_ok: Iterable[str] = (),
    now: datetime | None = None,
    stale_after: timedelta = timedelta(minutes=10),
) -> list[Problem]:
    """Return every way this incident's trail is incomplete or inconsistent."""
    now = now or datetime.now(timezone.utc)
    problems: list[Problem] = []
    ordered = sorted(records, key=lambda record: record.created_at)

    problems += _check_runs(ordered, now, stale_after)
    problems += _check_syntheses(ordered)
    problems += _check_dispatches(ordered)
    problems += _check_redaction(ordered)
    problems += _check_agent_coverage(ordered, required_agents, set(unaudited_ok))
    problems += _check_lifecycle(ordered)
    return problems


def _check_runs(ordered: Sequence[AuditRecord], now: datetime, stale_after: timedelta) -> list[Problem]:
    problems: list[Problem] = []
    open_run: dict[str, AuditRecord] = {}
    for record in ordered:
        if record.event_type == "agent_started":
            previous = open_run.get(record.actor)
            if previous is not None:
                problems.append(
                    Problem("unfinished_run", record.actor, f"run started {previous.created_at:%H:%M:%S} has no terminal event")
                )
            open_run[record.actor] = record
        elif record.event_type in TERMINAL:
            if open_run.pop(record.actor, None) is None:
                problems.append(Problem("orphan_terminal", record.actor, f"{record.event_type} without a start"))
    for actor, started in open_run.items():
        if now - started.created_at > stale_after:  # a recent open run is simply still running
            problems.append(Problem("unfinished_run", actor, f"run started {started.created_at:%H:%M:%S} never finished"))
    return problems


def _check_syntheses(ordered: Sequence[AuditRecord]) -> list[Problem]:
    problems: list[Problem] = []
    events = [record for record in ordered if record.event_type in SYNTHESIS]
    ids = [record.payload.get("report_revision_id") for record in events]
    for record, revision_id in zip(events, ids, strict=True):
        if not revision_id:
            problems.append(Problem("missing_revision_id", record.actor, f"{record.event_type} has no report_revision_id"))
    for revision_id, count in Counter(i for i in ids if i).items():
        if count > 1:
            problems.append(Problem("duplicate_revision", "coordinator", f"{revision_id} audited {count} times"))
    seen_initial: set[object] = set()
    for record in events:
        attempt = record.payload.get("attempt", 0)
        if record.event_type == "synthesis_completed":
            if attempt in seen_initial:
                problems.append(Problem("second_initial_synthesis", record.actor, f"attempt {attempt} has two initial syntheses"))
            seen_initial.add(attempt)
        elif attempt not in seen_initial:
            problems.append(Problem("revision_without_initial", record.actor, f"attempt {attempt} revised before it was reported"))
    return problems


def _check_dispatches(ordered: Sequence[AuditRecord]) -> list[Problem]:
    """Every agent in a live (not recovered) initial report was dispatched, with a valid outcome."""
    problems: list[Problem] = []
    dispatched: dict[tuple[object, str], str] = {}
    for record in ordered:
        if record.event_type != DISPATCH:
            continue
        outcome = record.payload.get("outcome")
        agent = str(record.payload.get("agent", ""))
        if outcome not in OUTCOMES:
            problems.append(Problem("bad_dispatch_outcome", agent or record.actor, f"outcome {outcome!r} is not one of {OUTCOMES}"))
        if record.duration_ms is None:
            problems.append(Problem("dispatch_without_duration", agent or record.actor, "agent_dispatch has no duration_ms"))
        dispatched[(record.payload.get("attempt", 0), agent)] = str(outcome)
    for record in ordered:
        if record.event_type != "synthesis_completed" or record.payload.get("reason") == "recovered":
            continue
        attempt = record.payload.get("attempt", 0)
        for agent in record.payload.get("agent_outcomes") or {}:
            if (attempt, agent) not in dispatched:
                problems.append(Problem("undispatched_outcome", agent, f"attempt {attempt} report lists {agent} but it was never dispatched"))
    return problems


def _check_redaction(ordered: Sequence[AuditRecord]) -> list[Problem]:
    problems: list[Problem] = []
    for record in ordered:
        text = json.dumps(record.payload, sort_keys=True, default=str)
        if redact(text) != text or redact(record.summary) != record.summary:
            problems.append(Problem("unredacted", record.actor, f"{record.event_type} carries redactable text"))
    return problems


def _check_agent_coverage(ordered: Sequence[AuditRecord], required: Iterable[str], unaudited_ok: set[str]) -> list[Problem]:
    """If the Coordinator reported an agent as completed, that agent's run must be in the trail."""
    outcomes: dict[str, str] = {}
    for record in ordered:
        if record.event_type in SYNTHESIS:
            outcomes.update(record.payload.get("agent_outcomes") or {})
    # An agent is audited if it wrote its own run, or the Coordinator audited its dispatch.
    started = {record.actor for record in ordered if record.event_type == "agent_started"}
    started |= {str(record.payload.get("agent")) for record in ordered if record.event_type == DISPATCH}
    problems = []
    for agent in required:
        if outcomes.get(agent) == "completed" and agent not in started and agent not in unaudited_ok:
            problems.append(Problem("unaudited_agent", agent, "reported completed but has no audited run or dispatch"))
    return problems


def _attempt(record: AuditRecord) -> int:
    value = record.payload.get("attempt", 0)
    return value if isinstance(value, int) else 0


def _check_lifecycle(ordered: Sequence[AuditRecord]) -> list[Problem]:
    """Request -> policy -> decision -> pull request -> re-validation -> status, in order and consistent."""
    problems: list[Problem] = []

    def warn(kind: str, actor: str, detail: str) -> None:
        problems.append(Problem(kind, actor, detail, severity="warning"))

    requested: set[int] = set()
    policy_denied: set[int] = set()
    policy_seen: set[int] = set()
    decisions: dict[int, AuditRecord] = {}
    pull_requests: set[int] = set()
    revalidation_ok = False
    revalidation_failed = False
    status: str | None = None
    last_attempt = 0

    for record in ordered:
        kind = record.event_type
        attempt = _attempt(record)
        if kind in ("approval_decided", "revalidation_completed", "incident_status_changed"):
            if attempt < last_attempt:
                problems.append(Problem("attempt_regressed", record.actor, f"{kind} for attempt {attempt} after attempt {last_attempt}"))
            last_attempt = max(last_attempt, attempt)

        if kind == "approval_requested":
            requested.add(attempt)
        elif kind == "policy_evaluated":
            policy_seen.add(attempt)
            if record.payload.get("allowed") is False:
                policy_denied.add(attempt)
        elif kind == "approval_decided":
            decision = record.payload.get("decision")
            decisions[attempt] = record
            if attempt not in requested:
                warn(GATE_WARNING, record.actor, f"attempt {attempt} was decided but no approval_requested was audited")
            if attempt not in policy_seen:
                warn(GATE_WARNING, record.actor, f"attempt {attempt} was decided but no policy_evaluated was audited")
            if decision == "approved" and attempt in policy_denied and not record.payload.get("human_override"):
                problems.append(Problem("approved_over_policy_denial", record.actor, f"attempt {attempt} approved although policy refused it (TC-15)"))
        elif kind == "pull_request_created":
            decided = decisions.get(attempt)
            if decided is None or decided.payload.get("decision") != "approved":
                why = "after a rejection" if decided is not None else "before any decision"
                problems.append(Problem("pr_without_approval", record.actor, f"pull request for attempt {attempt} created {why}"))
            pull_requests.add(attempt)
        elif kind == "revalidation_completed":
            if record.ok:
                revalidation_ok = True
            else:
                revalidation_failed = True
            if attempt not in pull_requests:
                warn(GATE_WARNING, record.actor, f"re-validation for attempt {attempt} has no audited pull_request_created before it")
        elif kind == "incident_status_changed":
            before, after = record.payload.get("from"), record.payload.get("to")
            if status is None and before != "OPEN":
                problems.append(Problem("status_chain_broken", record.actor, f"first status change starts from {before!r}, not OPEN"))
            elif status is not None and before != status:
                problems.append(Problem("status_chain_broken", record.actor, f"change from {before!r} but the previous change ended at {status!r}"))
            if status in TERMINAL_STATUSES:
                problems.append(Problem("reopened_after_terminal", record.actor, f"{status} changed again to {after!r}"))
            if after == "RESOLVED" and not revalidation_ok:
                problems.append(Problem("resolved_without_revalidation", record.actor, "marked RESOLVED with no passing re-validation before it"))
            if after == "ESCALATED" and not revalidation_failed:
                problems.append(Problem("escalated_without_failures", record.actor, "marked ESCALATED with no failed re-validation before it"))
            if after == "CLOSED_REJECTED" and not any(r.payload.get("decision") == "rejected" for r in decisions.values()):
                problems.append(Problem("closed_without_rejection", record.actor, "marked CLOSED_REJECTED with no rejection decision before it"))
            status = str(after)

    created = any(record.event_type == "incident_created" for record in ordered)
    reported = any(record.event_type in SYNTHESIS for record in ordered)
    if reported and not created:
        warn("legacy_trail", "backend", "has a report but no incident_created event (the incident predates the lifecycle audit)")
    return problems


__all__ = ["Problem", "check_incident", "errors"]
