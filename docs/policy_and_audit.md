# Policy and audit: what is guaranteed, where, and what is not

**Owner:** P2 (Week 8). The system proposes a code change to a failing build. This document says why that cannot go
wrong silently: three layers, each enforced in code and pinned by tests, each leaving a record. It then says plainly
what is *not* guaranteed. It joins `policies/README.md` (the rules themselves, P3), `docs/audit_workflow.md` (the
events and the checker) and `docs/decisions.md` (the standing choices).

## The three layers

```
Coordinator report  ->  [1] remediation policy  ->  [2] a named human decides  ->  [3] pull-request policy  ->  draft PR
      (what broke)       is this fix allowed         approve / reject, once          is there a real approval        Jenkins
                         to exist at all?            per attempt, as themselves      and is it a draft?              re-validates
                                         every step writes an audit event; none can be skipped silently
```

| Layer | Question | Decided by | Fails | Code |
|---|---|---|---|---|
| 1. Remediation policy | May this proposal be put in front of a reviewer? | OPA (`jenkinsguardians.remediation`) | **closed**: if OPA cannot answer, the proposal is blocked | `policies/remediation.rego`, `coordinator/policy.py`, `coordinator/remediation_gate.py` |
| 2. Human decision | Does a named person approve this exact attempt? | a signed-in reviewer or admin | refuses a second decision, a blank reviewer, an approval of a policy-blocked fix | `backend/routes/approvals.py::apply_decision`, `backend/ui/incidents.py` |
| 3. Pull-request policy | Is there a real approval, and is this a draft? | OPA (`jenkinsguardians.pullrequest`) | **closed**: no input a machine can supply on its own satisfies it | `policies/pull_request.rego`, `.github/workflows/draft-pr.yml` |

## What each guarantee rests on

| Guarantee | Enforced by | Pinned by | Recorded as |
|---|---|---|---|
| A report with a missing, failed or timed-out agent is never acted on | `IncidentReport.actionable` requires `complete` | `tests/coordinator`, `tests/scripts/test_evaluate_coordinator.py` | `synthesis_completed` (completeness, agent outcomes) |
| A low-confidence or unknown verdict is never acted on | the 0.5 confidence floor, shared by the Coordinator and the Rego rule | `tests/coordinator/test_policy_floor.py` | `synthesis_completed`, `policy_evaluated` |
| A fix is minimal and confined to the application under test | `remediation.rego`: at most 5 files / 40 lines, only `target_app/`, no protected or secret-bearing path, no deleted or silenced test | `policies/remediation_test.rego`, `tests/code_agent` | `remediation_proposed`, `policy_evaluated` (violations kept, also when blocked) |
| The agent cannot edit the rules that constrain it | `policies/`, `Jenkinsfile`, `.github/`, `jenkins/`, `docker-compose.yml`, `migrations/` are protected paths | `policies/remediation_test.rego` | `policy_evaluated` |
| A policy-blocked fix cannot be approved by accident (TC-15) | `apply_decision` refuses it unless `human_override` is passed; the web UI allows that only for an **admin** | `tests/ui/test_incidents.py`, `tests/test_backend_approvals.py` | `approval_decided` (`human_override` in the payload) |
| One decision per incident attempt, by a named person | unique constraint `(incident_id, attempt)`; the reviewer is the signed-in account, never a typed name | `tests/ui`, `tests/test_backend_approvals.py` | `approval_decided` |
| Only reviewers and admins decide; viewers read | role check `viewer < reviewer < admin`; project membership limits what a user sees | `tests/ui/test_auth.py`, `tests/ui/test_projects.py` | `user_*`, `project_*`, `approval_decided` |
| A rejected fix ends the incident and nothing is opened | rejection closes it (`CLOSED_REJECTED`) and no retry is scheduled | `tests/ui/test_lifecycle.py` | `approval_decided`, `incident_status_changed` |
| No pull request without an approval, and only as a draft | `pullrequest.rego` (named human, draft, no auto-merge); the live workflow passes `--draft` | `policies/pull_request_test.rego`, `tests/integration/test_remediation_loop.py` | `pull_request_created` |
| A failed fix is retried a bounded number of times, then handed to a human | `MAX_ATTEMPTS` (default 2), then status `ESCALATED` | `tests/test_backend_validation.py` | `revalidation_completed`, `incident_status_changed` |
| Secrets do not reach evidence, reports, audit or comments | redaction on audit records, on evidence **when written** (whatever the agent claimed), on reviewer comments | `tests/security/test_tc20_secret_redaction.py`, `tests/common/test_evidence_store.py` | `unredacted` is an audit-check error |
| The record itself is consistent | the lifecycle rules of `common/audit_check.py` (a PR before approval, an approval over a policy refusal, a broken status chain are errors) | `tests/common/test_audit_check.py` | `python scripts/check_audit.py --all` |

## The audit trail

Append-only (`audit_events`, or `data/audit/<incident>.jsonl`): a later outcome is a later record, never an update.
Every record's summary and payload are redacted before they are written. An incident's outcome (`resolved_by_fix`,
`rejected`, `escalated`, ...) is *derived* from the trail by `monitoring/sql/incident_outcomes.sql` rather than stored
twice. The events, their order and the checker's rules are in `docs/audit_workflow.md`.

How to answer "who did what, and why" for any incident: open it in the web UI (the timeline lists every audited step,
and the decision names the reviewer and their comment), or run `python scripts/check_audit.py <incident-id>`.

## What is not guaranteed

Read this part before relying on any claim above.

- **The live pull-request path does not yet write the gate's audit events.** `RemediationGate` records the proposal, the
  policy result, the approval request and the pull request; only the demo script uses it. A live incident therefore
  shows the decision and re-validation but not those four events, and the audit check reports a `gate_not_in_live_path`
  **warning**, not an error. The guarantees in layers 1 and 3 are enforced; their *record* is incomplete in the live path.
- **Correctness of the verdict is not guaranteed.** The policy decides whether a fix is *allowed*, not whether it is
  *right*. The first live injected fault was diagnosed wrongly and its report was still marked actionable. Accuracy is
  measured, not asserted: see `results/coordinator_eval.md` (fusion, simulated agents) and `scripts/evaluate_end_to_end.py`
  (real injected faults). The human decision in layer 2 is the safeguard against a wrong verdict, which is why it cannot be skipped.
- **A person can override a policy refusal.** `human_override` exists for someone who has read the diff and accepts the risk.
  The web UI allows it for admins only; the bearer-token API treats whoever holds the token as authorised. Either way it is
  recorded in the audit event, but it is a person's decision, not a machine check.
- **The audit trail can be read by anyone with database access.** It is tamper-*evident* only through the consistency
  checks; there is no cryptographic chain. Treat database write access as equivalent to editing the history.
- **A person can sign in as any account they know the password of.** Accounts are local, with a rate-limited login and
  no multi-factor step. Approval links sent by email are signed and single-use, but anyone holding the mail holds the link.
- **Embedded Grafana is not project-isolated.** All signed-in users share one read-only Grafana identity and can change
  the project filter in the dashboard. The charts on a project's Overview page, which come from the access-controlled
  API, are the isolated view.
- **Evidence is stored in two places** (the blackboard and `data/evidence`) and merged on read; retention is manual
  (`scripts/prune_evidence.py`).

## Verifying all of it

```bash
.venv/Scripts/python -m pytest tests -q --ignore=tests/test_audit_migration.py   # the guarantees above, as tests
scripts/opa.cmd test policies                                                      # the Rego rules (Docker, no local opa)
.venv/Scripts/python scripts/check_audit.py --all --errors-only                    # the live trail against the lifecycle rules
.venv/Scripts/python scripts/check_dashboards.py                                   # every dashboard panel against the real stack
```
