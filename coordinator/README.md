# Coordinator

**Owner:** P3. Owns incident-level synthesis of independent agent evidence and the
decisions that follow from it.

The investigation agents never talk to each other. The Coordinator is the only
component that reads all three evidence documents for an incident, fuses them into one
report, and drives what happens next.

```text
agents --A2A evidence.ready--> CoordinatorAgent --collect--> blackboard
                                      |
                                  synthesize --> IncidentReport
                                      |
                       (code agent) propose_fix --> FixPlan | NoFix
                                      |
                              RemediationGate
        submit:  remediation policy ----------> policy_rejected | awaiting_approval
        decide:  human approval -> PR policy -> branch -> draft PR  (or rejected / pr_blocked)
```

## Coordinator agent (Google ADK)

`agent.py` is a Google ADK `BaseAgent`. It receives A2A notifications and reads the
blackboard. `collect` returns what each agent concluded, unchanged and unranked;
`synthesize` fuses it. The two stay separate so a reviewer can always see the agents'
own words before the Coordinator's verdict.

## Synthesis (Week 4)

`synthesis.py` turns every agent's evidence for one incident into an `IncidentReport`
- Pydantic structured output, rendered to Markdown for a reviewer. Deterministic, so
it is the baseline LLM-assisted fusion will be measured against.

| Rule | Effect |
| --- | --- |
| Agreement | each additional agent naming the same cause adds **0.1** |
| Disagreement | each competing cause costs **0.15**, and is named in the report |
| A tie on agent count | `conflicting` when the strongest claims are within **0.10** (`TIE_MARGIN`); a wider gap is a lead, and the losing cause is still recorded and costs confidence |
| Completeness | `missing_agents` and `failed_agents` say who did not contribute; `complete` is independent of `actionable` |
| Abstention | insufficient-evidence and failed agents are findings, never votes |
| Floor | below **0.5** the report names no cause and is not `actionable` |
| Ceiling | the fused verdict never exceeds **0.95** - agents can share a blind spot, so agreement is strong evidence, not proof |

Cited evidence IDs are namespaced by agent (`code_agent:file-1`), because each agent
numbers its evidence independently and `file-1` is ambiguous once merged.

The running service (`app.py`) calls `synthesize` after every dispatch and keeps the
latest report per incident in `CoordinatorRuntime.reports`. Fusion there is best effort:
the dispatch has already completed, so a fusion error is logged and never re-raised.

## Remediation gate (Week 6)

`remediation_gate.py` is the only path from a proposed fix to GitHub, through three
gates in order:

1. **Remediation policy** - is the fix safe and minimal enough to show a human?
   Blocks unsafe fixes such as skipping a test (TC-15) before anyone is asked.
2. **A recorded human decision** - approve, reject or request changes, with a named
   reviewer. A rejection ends it: no branch, no PR (TC-16).
3. **Pull-request policy** - re-checks the policy pass, the named approval, that the
   PR is a draft and that auto-merge is off.

Only then is a branch created and a **draft** pull request opened (TC-11). `submit`
and `decide` are separate calls because a human answers when they answer. Every step
is written to the audit trail, refusals included:

`remediation_proposed → policy_evaluated → approval_requested → approval_decided → pull_request_created`

Every gate fails closed. An unreachable policy engine blocks the proposal; a fix that
went stale between approval and branching is recorded as `failed`, never forced.

## Policy gate

`policy.py` evaluates `policies/` two ways, sharing one parser so they cannot disagree:

* `PolicyGate` queries the running `opa` service over HTTP.
* `OpaCliGate` runs the `opa` CLI with no server - the Conftest-style path for CI and
  for tests that check proposals against the real Rego.

A `PolicyVerdict` carries `allowed`, the `violations` that blocked it and any
`warnings`. If OPA cannot answer, both raise `PolicyUnavailable` rather than returning
a permissive verdict. An undefined package returns no result, which is not an allow.

Note that a denying `PolicyVerdict` is falsy - its `__bool__` is `allowed`. Test it
with `is None`, never with `if verdict:`, or a denial reads as "no verdict".

Rules live in `policies/` and are documented there; see `policies/README.md` before
changing one.

## Fused report (P2, alongside `synthesis.py`)

`synthesis.py` (P3) is the report the remediation engine and the policy gate read. P2 added
a second, more detailed fusion next to it so the two can be compared before either is made
authoritative: `fusion.py`, `report.py` (`FusedReport`), `report_store.py` and `fused.py`
(`synthesize_fused`: collect, fuse, store, audit `fused_report_completed`, kept apart from the authoritative `synthesis_completed` / `synthesis_revised` events the running Coordinator writes). Neither alters the
agents' evidence.

`fuse(view, config, now=...)` is pure and deterministic, independent of the order the
agents' documents arrive in:

- **One vote per agent**: its primary verdict. Other hypotheses it listed are shown in the
  report (`alternative_hypotheses`) but not scored.
- **Score** = noisy-OR (default) of `confidence x relevance`; the weighted-mean score is
  always recorded alongside (`docs/fusion_method_comparison.md`). **Uncalibrated, not
  probabilities.**
- **Relevance** (`AGENT_RELEVANCE`) and **type relations** (`TYPE_RELATIONS`) are
  *proposals pending team sign-off*; a relation not signed off counts as unclassified,
  which is treated as a conflict and tagged, unless `trust_proposed_relations` is set.
- **Negative evidence** is gated (relevant agent, not failed, declared
  `investigation_coverage >= min_coverage` for "found nothing"), deduplicated by
  `(agent, failure_type, evidence id)`, and capped (0.30).
- **Separate fields**: `verdict`, `completeness`, `conflict_status` and
  `ready_for_policy_review`, a precondition flag only. OPA stays authoritative.
- **No vote -> `unknown`** at confidence 0, never a winner from zero scores.

Replay a stored incident without the stack:

```bash
python scripts/synthesize_incident.py <incident_id>
```

### Optional A2A listener (`service.py`, `a2a_client.py`)

`service.py` is an A2A endpoint that waits for all three agents (read from the blackboard)
or `COORDINATOR_TIMEOUT_SECONDS`, then writes the fused report; a late agent replaces an
incomplete one. `a2a_client.A2ANotifier` is the matching sender. **Neither is wired into
`docker-compose.yml` or the agents**: P3's `app.py` dispatcher is the live path. Pending
timers are in memory and the endpoint has no authentication.
## A2A contract

The contract lives in `common/a2a_contract.py`, not here: every agent imports it, and
each agent image ships only its own package plus `common/`. It defines the agent cards
and the one message kind the Coordinator accepts, `evidence.ready`, which carries the
incident id and agent name and **no findings**. A2A carries notification, not payload,
so no agent can hand another agent a conclusion - the blackboard stays the only route
evidence travels.

## Frameworks

The three investigation agents run on CrewAI; this Coordinator runs on Google ADK.
CrewAI orchestrates each investigation, ADK coordinates across them, and A2A is the
boundary between the two.

## Dispatch, outcomes and report revisions (P2, Week 5)

`CoordinatorRuntime.dispatch_all` (`app.py`) is the single synthesis trigger. For each required agent
(`COORDINATOR_REQUIRED_AGENTS`, default all three) it records one outcome - `completed`, `failed`,
`timed_out` or `not_dispatched` - then synthesizes once. Agents in `COORDINATOR_A2A_AGENTS` are sent an
authenticated `POST /tasks` (3 tries, 504 = `timed_out` at once); any other required agent triggers itself
from RabbitMQ and is *watched* on the blackboard for at most `COORDINATOR_WATCH_TIMEOUT_SECONDS`. It is
idempotent per `(incident_id, attempt)`.

`TRIGGER_MODE` says who starts an investigation and is set identically on the Coordinator, the Metrics agent
and the Code agent: `a2a` (default) - the Coordinator sends each agent a task and the agents serve A2A
(`a2a_app.py` in each agent), their RabbitMQ consumers are not started; `rabbitmq` - the Metrics and Code
consumers investigate and the Coordinator only watches for their evidence.

A report is **complete** only if every required agent is `completed` *and* has evidence; `failed`,
`timed_out`, `not_dispatched`, or `completed` without evidence all make it incomplete, and an incomplete
report is never `actionable`.

`revisions.py` persists each synthesis as a revision (`data/reports/<incident>/a<attempt>-r<N>.json`) with
`report_revision_id = <incident>:<attempt>:<evidence fingerprint>`. The initial one is audited as
`synthesis_completed`, later ones (an agent that reports after the deadline, within
`COORDINATOR_LATE_WINDOW_SECONDS`) as `synthesis_revised`. The same evidence is never persisted twice, a
result for an older attempt never replaces the current attempt's report, and on start `recover()` repairs a
revision whose audit event was lost and synthesizes recently-changed incidents that have no report - safe to
run any number of times. Check a trail with `python scripts/check_audit.py --all`.

Authority: `IncidentReport.actionable` (and OPA) act; the fused report's `ready_for_policy_review` only
advises and `apply_gate` forces it off whenever `actionable` is false.

