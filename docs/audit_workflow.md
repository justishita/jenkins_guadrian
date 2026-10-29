# Audit workflow: what is recorded, in what order, and how it is checked

**Owner:** P2 (Week 7). The audit trail is the `audit_events` table (PostgreSQL) or `data/audit/<incident>.jsonl`
(files). It is append-only: a later outcome is a later record, never an update. Every record's summary and payload
are redacted before they are written (`common/audit.py`).

## An incident's life, event by event

```
incident_created                      backend, when the webhook creates the incident
  agent_started / tool_call / hypothesis_formed / evidence_written / agent_completed|agent_failed
                                      each investigating agent (one run = a start and one terminal event)
  agent_dispatch                      Coordinator: the outcome and duration of each agent it asked
  synthesis_completed                 the initial report; later changes are synthesis_revised
remediation_proposed                  a fix was proposed          \
  policy_evaluated                    the policy said yes or no    |  written by the RemediationGate
  approval_requested                  a human is asked             |
approval_decided                      approved or rejected (backend)
  pull_request_created                draft PR opened after approval /
revalidation_completed                Jenkins re-ran the build; ok = it passed
incident_status_changed               OPEN -> RESOLVED | OPEN -> ESCALATED | OPEN -> CLOSED_REJECTED
                                      (also OPEN -> OPEN: a failed re-validation starts another attempt)
```

How an incident ends:

| Outcome | What happened |
|---|---|
| `resolved_by_fix` | a fix was approved and Jenkins re-validation passed (`RESOLVED`) |
| `rejected` | a human rejected the fix; no retry is scheduled, so the incident closes (`CLOSED_REJECTED`) |
| `escalated` | re-validation failed on every attempt until `MAX_ATTEMPTS` was used up (`ESCALATED`) |
| `awaiting_validation` / `awaiting_approval` / `policy_blocked` / `open` | still in progress, or no fix was proposed |

The outcome is derived, not stored: `monitoring/sql/incident_outcomes.sql` computes it from `incidents`,
`audit_events` and the proposals. Every status change goes through `backend/incident_lifecycle.py`, which writes the
audit row in the same transaction as the status.

## The checks (`python scripts/check_audit.py --all`)

Rules live in `common/audit_check.py`. An **error** fails the check (exit status 1); a **warning** is reported only.

| Rule | Kind | What it catches |
|---|---|---|
| `unfinished_run`, `orphan_terminal` | error | an agent start without an end, or an end without a start |
| `duplicate_revision`, `missing_revision_id`, `second_initial_synthesis`, `revision_without_initial` | error | report revisions that are not unique or out of order |
| `bad_dispatch_outcome`, `dispatch_without_duration`, `undispatched_outcome`, `unaudited_agent` | error | the Coordinator's dispatch records are missing or invalid |
| `unredacted` | error | a record carries text that redaction would have masked |
| `pr_without_approval` | error | a pull request before any decision, or after a rejection |
| `approved_over_policy_denial` | error | approved although policy refused it, without a recorded human override (TC-15) |
| `resolved_without_revalidation`, `escalated_without_failures`, `closed_without_rejection` | error | a final status that the events before it do not justify |
| `status_chain_broken`, `reopened_after_terminal` | error | status changes that do not follow on from each other, or an incident reopened after it ended |
| `attempt_regressed` | error | a later event for an earlier attempt |
| `gate_not_in_live_path` | warning | a decision or re-validation with no audited request, policy result or pull request |
| `legacy_trail` | warning | a report but no `incident_created` (the incident predates the lifecycle audit) |

### Known gap: the live path does not write the gate's events

`RemediationGate` (`coordinator/remediation_gate.py`) writes `remediation_proposed`, `policy_evaluated`,
`approval_requested` and `pull_request_created`, but only `scripts/demo_p3.py` uses it. The live pull-request path
(`.github/workflows/draft-pr.yml`) writes none of them, so on a live incident those events are absent. The check
reports that as the `gate_not_in_live_path` warning rather than as an error, so real violations are not hidden by it.
Closing the gap means the remediation owner (P3) calling the gate, or writing the same events, from the live path;
that is outside this document's scope.

Also not written anywhere yet: `llm_call` (so there are no LLM cost or latency panels) and `incident_linked`.

## Where to look

- Grafana: **Agent Ops** (investigations) and **Approvals & Operations** (what is waiting, how incidents end); alerts
  under Alerting. See `monitoring/README.md`.
- The web UI: a project's Overview shows how its incidents ended; an incident's page shows its timeline.
- `python scripts/check_audit.py <incident_id>` for one incident; `--errors-only` hides warnings.
