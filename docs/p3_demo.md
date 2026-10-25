# P3 demo runbook

**Owner:** P3. How to show the Code agent, Coordinator, remediation engine and policy
gate working end to end, on a laptop with no Docker.

## Before you present (once)

```bash
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python scripts\get_opa.py
```

Then, just before presenting, run both of these. If either fails, do not demo blind.

```bash
.venv\Scripts\python scripts\demo_p3.py
.venv\Scripts\python -m pytest -q
```

Expected: the demo prints `All scenarios behaved as expected.` and exits 0. The suite
reports **1 failed** - `target_app/tests/test_intentional_failure.py`, a deliberate
`assert False` that exists so Jenkins always has a failure to investigate - and
**1 skipped**, the metrics agent's PostgreSQL test, which needs `TEST_DATABASE_URL`.
Everything else passes.

## What is real and what is simulated

Say this up front; it pre-empts the obvious question.

| Real code | Simulated |
| --- | --- |
| CrewAI code agent and its reasoning | GitHub - an in-memory fake, so no repository is touched |
| A2A `evidence.ready` notifications | Jenkins and metrics agents' evidence - recorded, labelled as such |
| Google ADK Coordinator, synthesis | |
| Remediation engine, branching workflow | |
| OPA decisions - the actual Rego in `policies/` | |
| The audit trail | |

The two simulated agents need a live Jenkins, Prometheus and an LLM key; their real
versions are covered by their own test suites.

## The five scenarios

| Scenario | What it shows | Ends in |
| --- | --- | --- |
| TC-03 | dependency bumped to a version that does not exist | a **draft** PR reverting exactly one line |
| TC-15 | a "fix" that skips the failing test | blocked by policy before any human is asked |
| TC-12 | the build times out but the commit changed nothing | cause found (timeout), fix **declined** |
| TC-16 | a valid fix the reviewer rejects | no branch, no PR, rejection in the audit trail |
| TC-19 | the same failure for the third time | report links the two earlier incidents and shows the recurrence count |

Two of the five end in "no". That is the point: the system's value is as much in what it
refuses to do as in what it does.

## Lines worth saying

* **The fix is the inverse of the breaking diff.** The removed line from the commit
  that broke the build replaces the added one, verbatim. Nothing else is touched.
* **Three gates, all failing closed.** Remediation policy, then a named human, then
  pull-request policy. If OPA cannot answer, the answer is no.
* **The PR is always a draft.** There is no code path that opens a ready PR or enables
  auto-merge, and the policy refuses both anyway.
* **Confidence never reaches 1.00.** Agents can share a blind spot, so the fused verdict
  is capped at 0.95 however many agree.
* **0.5 is enforced in three places** - the code agent, synthesis and the Rego - and
  tests assert they stay equal.

## If something goes wrong live

| Symptom | Cause | Fix |
| --- | --- | --- |
| `opa not found` | `.bin/` missing | `python scripts\get_opa.py` |
| `UnicodeEncodeError` from CrewAI | an old checkout without `suppress_flow_events` | pull `main` |
| `HTTP_413_CONTENT_TOO_LARGE` error | FastAPI older than 0.162 in the venv | reinstall from `requirements-dev.txt` |
| a scenario prints `FAIL` | real regression | do not demo it; the test suite will name it |
