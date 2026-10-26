"""Audit-trail completeness check, per run rather than per incident.

**Owner:** P2 (Week 5). An incident can legitimately have several runs of the same agent
(redelivery, reinvestigation), so the unit checked is the *run*: every ``agent_started`` must
be followed by exactly one terminal event (``agent_completed`` or ``agent_failed``) before that
actor's next start. Report syntheses are checked the same way: each persisted revision has
exactly one audit event keyed by ``report_revision_id``.

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


@dataclass(frozen=True)
class Problem:
    kind: str  # unfinished_run | orphan_terminal | duplicate_revision | missing_revision_id | ...
    actor: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.actor}: {self.detail}"


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


__all__ = ["Problem", "check_incident"]
