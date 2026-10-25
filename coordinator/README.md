# Coordinator

**Owner:** P3. Owns incident-level synthesis of independent agent evidence and the
decisions that follow from it.

The investigation agents never talk to each other. The Coordinator is the only
component that reads all three evidence documents for an incident, fuses them into one
report, and drives what happens next.

## Policy gate

`policy.py` is the client for the OPA guardrails in `policies/`. It evaluates a
proposed remediation before a reviewer sees it, and the approval state before a pull
request is opened:

```python
async with PolicyGate(settings.OPA_URL) as gate:
    verdict = await gate.evaluate_remediation(proposal)
    if not verdict.allowed:
        ...  # record verdict.violations and stop
```

A `PolicyVerdict` carries `allowed`, the `violations` that blocked it and any
`warnings` for the reviewer. If OPA cannot be reached the call raises
`PolicyUnavailable` rather than returning a permissive verdict: the proposal is
blocked. Every verdict, allow or deny, belongs in the `policy_decisions` audit table.

Rules live in `policies/` and are documented there; see `policies/README.md` before
changing one.

## Coordinator agent (Google ADK)

`agent.py` is a Google ADK `BaseAgent`. It is the only component allowed to see all
three evidence documents, and it is deliberately a **collection layer only**: it
receives A2A notifications, reads the blackboard and reports what each agent
independently concluded. It does not fuse evidence, resolve conflicting hypotheses or
produce an incident report - that synthesis is not implemented, and inventing it here
would be guessing.

`BlackboardView` reports which agents have written, which are still missing, and each
agent's verdict unchanged. A test asserts the view carries no combined `failure_type`
or `confidence`, so fusion cannot be added by accident.

## A2A contract

`a2a_contract.py` defines the agent cards and the one message kind the Coordinator
accepts, `evidence.ready`. The notification carries the incident id and the agent name
and **no findings** - a receiver that wants the evidence reads it from the shared
store. That split is what keeps the blackboard architecture intact: A2A carries
notification, not payload, so no agent can hand another agent a conclusion.

Malformed messages raise `A2AContractError` rather than being skipped; a notification
that cannot be parsed is a contract break worth surfacing.

## Frameworks

The three investigation agents run on CrewAI; this Coordinator runs on Google ADK.
The split is real rather than cosmetic: CrewAI orchestrates each investigation,
ADK coordinates across them, and A2A is the boundary between the two.
