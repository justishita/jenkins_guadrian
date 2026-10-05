# Code Agent

**Owner:** P3. Investigates whether a recent code change explains a build failure, and
from Week 5 proposes the minimal fix for it. One of three agents that investigate an
incident independently; it never calls another agent, and it communicates only by
writing one evidence document to the shared store.

## Flow

```text
incident.created -> fetch_context -> analyze -> reason -> finalize
   (RabbitMQ)        GitHub reads    signals   ranked    Evidence
                                               causes    + audit
```

The agent binds its own durable queue, `code_agent.incident.created`, to the shared
`incidents` exchange on routing key `incident.created`, so all three agents receive
their own copy of every event.

* **fetch_context** reads the failing commit and its files, the pull requests that
  commit belongs to, and recent branch history. Partial retrieval is normal: a commit
  GitHub cannot show still leaves the history to reason over, and only a total failure
  is reported as a retrieval failure.
* **analyze** classifies every changed path by role — dependency manifest,
  configuration, CI pipeline, deployment, test, source, documentation — and extracts
  the signals that matter: dependency pins that moved, configuration values that
  changed.
* **reason** ranks the plausible code-side causes and decides what to report. Week 3
  implements this as deterministic rules rather than an LLM call: the rules are the
  ground truth the later LLM reasoning is evaluated against, they run in the scenario
  tests without a network or an API quota, and a rule that fires wrongly can be read
  and corrected.
* **finalize** writes one `Evidence` document under `data/evidence/<incident>/code_agent.json`
  and records the run in the audit trail.

An investigation always produces a document, including when it crashes or GitHub is
unreachable. "This agent ran and found nothing" and "this agent never reported" mean
different things to the Coordinator.

## Reasoning

Each hypothesis carries a confidence and cites the evidence IDs it rests on;
`EvidenceIndex` hands out those IDs so the document and the hypotheses cannot disagree
about one. Two restraints matter more than any individual rule:

* **Absence of code evidence is evidence.** When nothing changed, or only
  documentation changed, the agent says a code change does not explain the failure
  rather than blaming the most recent commit it can see (TC-12).
* **A competing explanation lowers confidence.** A commit that moves a dependency pin
  *and* edits ten source files is weak evidence for either, and the score says so, so
  the Coordinator weighs it accordingly (TC-13).

Below `MIN_REPORTABLE_CONFIDENCE` (0.5) the agent reports `insufficient_evidence` and
asks for correlation instead of naming a cause (TC-14). That floor is the same number
as the policy gate's minimum, so the agent never proposes a fix from evidence OPA
would refuse to act on. `unknown` is capped at 0.4 — a statement of ignorance may not
be made confidently.

## Scenarios

Ground truth lives in `scenarios/` and is executed against the real agent by
`tests/code_agent/test_scenarios.py`. The code agent currently covers TC-03
(dependency regression), TC-08 (configuration error), TC-12 (anomaly without a code
change) and TC-14 (no clear root cause). To produce a real failed build rather than a
recorded one:

```bash
python scripts/inject_code_faults.py tc03    # or tc08
python scripts/inject_code_faults.py revert
```

## Status

Weeks 2 and 3 deliver retrieval, analysis and rule-based root-cause reasoning. Week 4
adds LLM-assisted reasoning and confidence calibration on top of the same evidence,
measured against these scenarios.

GitHub access is **read-only** until Week 5. Draft pull requests arrive then, behind
the OPA policy gate in `policies/` and a recorded human approval.

## Configuration

| Variable | Purpose |
| --- | --- |
| `GITHUB_TOKEN` | Fine-grained token with read access to contents and pull requests |
| `GITHUB_REPO` | `owner/name` of the repository under CI — not a URL |
| `GITHUB_API_URL` | Defaults to `https://api.github.com` |
| `RABBITMQ_URL` | Shared broker |
| `COMMIT_LOOKBACK` | How many recent commits to consider (default 10) |
| `EVIDENCE_DIR` / `AUDIT_DIR` | Where evidence and the trail are written |
| `OPA_URL` | Policy service, used from Week 5 |

All of it is validated at startup, so a missing token fails the container immediately
rather than halfway through the first incident.

## Safety

Every commit message, patch and file body is passed through `common.redaction.redact`
inside the client, before it reaches an LLM, the evidence store or the audit trail —
a diff is one of the easier places for a credential to end up. Oversized patches are
truncated rather than dropped, so the line counts survive even when the content does
not.

## Tests

```bash
python -m pytest tests/code_agent
```

No network and no LLM: the GitHub client is driven through a mock transport, and the
diff analysis is pure functions over `FileChange` values.
