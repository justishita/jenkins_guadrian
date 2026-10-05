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
