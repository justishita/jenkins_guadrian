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
three evidence documents. `collect()` is the collection layer: it reads the blackboard
and returns a `BlackboardView` with what each agent independently concluded, unchanged
and unranked. A test asserts the view carries no combined `failure_type` or
`confidence`, so fusion cannot leak into it.

`synthesize(incident_id)` (added by P2) chains `collect -> fuse -> ReportStore -> audit`
and returns an `IncidentReport`. The agents' evidence is never modified.

## Fusion and the incident report (P2)

`fusion.py` is a pure, deterministic function: `fuse(view, config, now=...)`. The result
is independent of the order the agents' documents arrive in.

- **One vote per agent**: its primary verdict. Other hypotheses it listed are shown in the
  report (`alternative_hypotheses`) but not scored, because their confidences are not
  comparable across agents.
- **Score** = noisy-OR (default) of `confidence x relevance`; the weighted-mean score is
  always recorded alongside. See `docs/fusion_method_comparison.md` for why noisy-OR is the
  provisional choice. **Scores are uncalibrated, not probabilities.**
- **Relevance** (`AGENT_RELEVANCE`) and the **type relations** used for conflict detection
  (`TYPE_RELATIONS`) are *proposals pending team sign-off*. A relation that is not signed
  off is treated as unclassified, which counts as a conflict and is tagged in the report,
  unless `FusionConfig.trust_proposed_relations` is set.
- **Negative evidence** is gated (relevant agent, not failed, declared
  `investigation_coverage >= min_coverage` for "found nothing"), deduplicated by
  `(agent, failure_type, evidence id)`, an explicit contradiction suppresses the general
  absence for the same agent and type, and the total discount is capped (0.30).
- **Separate fields**: `verdict`, `completeness` (+ missing/failed agents),
  `conflict_status`, and `ready_for_policy_review`, a precondition flag only. OPA stays
  authoritative; nothing here approves a remediation.
- **No vote -> `unknown`** with confidence 0; never a winner chosen from zero scores.

`report.py` holds the models and the Markdown renderer; `report_store.py` writes
`data/reports/<incident_id>.{json,md}`.

Replay a stored incident without the stack:

```bash
python scripts/synthesize_incident.py <incident_id>
```

## Running as a service (live A2A)

`service.py` hosts the Coordinator as an A2A endpoint (compose service `coordinator`,
port 8090). The agents' `notifier` is wired from `COORDINATOR_URL` (see `a2a_client.py`);
unset, notifications are disabled. For each incident the service waits until all three
agents have written (checked on the blackboard) or `COORDINATOR_TIMEOUT_SECONDS`, then
synthesizes; on timeout the report is `incomplete`, and a late agent replaces it with a
fresh one. Pending timers are in memory only and the endpoint has no authentication.

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
