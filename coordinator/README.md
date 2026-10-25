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
| A tie on agent count | `conflicting` - confidence orders candidates but never breaks a tie |
| Abstention | insufficient-evidence and failed agents are findings, never votes |
| Floor | below **0.5** the report names no cause and is not `actionable` |

Cited evidence IDs are namespaced by agent (`code_agent:file-1`), because each agent
numbers its evidence independently and `file-1` is ambiguous once merged.

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
