# Code Agent

**Owner:** P3. Investigates whether a recent code change explains a build failure, and
from Week 5 proposes the minimal fix for it. One of three agents that investigate an
incident independently; it never calls another agent, and it communicates only by
writing one evidence document to the shared store.

## Flow

```text
incident.created  ->  fetch_context  ->  analyze  ->  finalize
   (RabbitMQ)         GitHub reads      diff signals   Evidence + audit
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
* **finalize** writes one `Evidence` document under `data/evidence/<incident>/code_agent.json`
  and records the run in the audit trail.

An investigation always produces a document, including when it crashes or GitHub is
unreachable. "This agent ran and found nothing" and "this agent never reported" mean
different things to the Coordinator.

## Status

Week 2 delivers retrieval and analysis. The agent reports `unknown` with low
confidence and no hypotheses — retrieval is not a root cause, and handing the
Coordinator a guess is worse than handing it nothing (TC-14). Week 3 adds the
root-cause reasoning that earns a classification, starting with TC-03 (dependency
regression) and TC-08 (configuration error).

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
