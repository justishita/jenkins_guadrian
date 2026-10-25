# Jenkins PR Build Trigger API

## Overview

When a remediation PR is created, Jenkins needs to build and test it. The
Multibranch Pipeline plugin auto-discovers branches via periodic scans, but this
can take up to **2 minutes**. For immediate feedback, trigger the build
explicitly via the Jenkins REST API.

## Trigger URLs

### Multibranch Pipeline (branch builds)

```
POST /job/<pipeline>/job/<branch>/build
```

Example:

```bash
curl -X POST \
  "http://jenkins:8080/job/target-app/job/main/build" \
  --user "$JENKINS_USER:$JENKINS_API_TOKEN"
```

### Multibranch Pipeline (PR / Change-Request builds)

The path for PR builds depends on the SCM plugin and view configuration. Common
patterns:

```
POST /job/<pipeline>/view/change-requests/job/PR-<n>/build
POST /job/<pipeline>/job/PR-<n>/build
```

Example:

```bash
curl -X POST \
  "http://jenkins:8080/job/target-app/view/change-requests/job/PR-7/build" \
  --user "$JENKINS_USER:$JENKINS_API_TOKEN"
```

> **Note:** Test the working URL in your instance and record it here. The exact
> path varies by Jenkins version, GitHub Branch Source plugin version, and
> Multibranch configuration.

### Freestyle / Pipeline (non-multibranch)

```
POST /job/<job-name>/build
```

### With Parameters

```
POST /job/<job-name>/buildWithParameters?BRANCH=ai-fix/abc123-1
```

## Verifying the Trigger URL in Your Instance

The exact PR trigger URL varies per Jenkins setup. Use these steps to discover
the working URL before wiring it into the agent or CI automation:

### Step 1 — List top-level jobs

```bash
curl -s -u "$JENKINS_USER:$JENKINS_API_TOKEN" \
  "$JENKINS_URL/api/json?tree=jobs[name,url]" | python -m json.tool
```

### Step 2 — Inspect the multibranch pipeline

```bash
# Replace target-app with your pipeline name
curl -s -u "$JENKINS_USER:$JENKINS_API_TOKEN" \
  "$JENKINS_URL/job/target-app/api/json?tree=jobs[name,url]" | python -m json.tool
```

Look for a `change-requests` view or PR sub-jobs in the output.

### Step 3 — Discover the view / PR path

```bash
# Check if the change-requests view exists
curl -s -u "$JENKINS_USER:$JENKINS_API_TOKEN" \
  "$JENKINS_URL/job/target-app/view/change-requests/api/json?tree=jobs[name,url]" \
  | python -m json.tool
```

If that returns 404, try the flat multibranch job list instead:

```bash
curl -s -u "$JENKINS_USER:$JENKINS_API_TOKEN" \
  "$JENKINS_URL/job/target-app/job/PR-7/api/json" | python -m json.tool
```

### Step 4 — Attempt the trigger and verify

```bash
# Try the view-based path first
curl -v -X POST \
  "$JENKINS_URL/job/target-app/view/change-requests/job/PR-7/build" \
  --user "$JENKINS_USER:$JENKINS_API_TOKEN"

# If 404, try the flat path:
curl -v -X POST \
  "$JENKINS_URL/job/target-app/job/PR-7/build" \
  --user "$JENKINS_USER:$JENKINS_API_TOKEN"
```

A **201 Created** response means the build was queued successfully.

### Step 5 — Record the working URL

Once you have found the URL that returns 201, record it here and in the
environment variable `JENKINS_PR_TRIGGER_URL_TEMPLATE` so the agent can use it
without re-discovery on every run.

Example:

```
JENKINS_PR_TRIGGER_URL_TEMPLATE=http://jenkins:8080/job/target-app/view/change-requests/job/{pr}/build
```

## Explicit Trigger via `trigger_build_url()`

To bypass the Multibranch scan delay, call `JenkinsClient.trigger_build_url()`
directly from the agent or coordinator:

```python
from agents.jenkins_agent.tools.jenkins_client import JenkinsClient

client = JenkinsClient()
await client.trigger_build_url(
    "http://jenkins:8080/job/target-app/view/change-requests/job/PR-7/build"
)
```

`trigger_build_url()` handles retries, CSRF tokens are **not** required when
using API tokens, and raises typed exceptions (`JenkinsAuthError`,
`JenkinsNotFound`, `JenkinsUnavailable`) on failure.

For CSRF-protected instances, fetch a crumb first:

```bash
CRUMB=$(curl -s "$JENKINS_URL/crumbIssuer/api/xml?xpath=concat(//crumbRequestField,\":\"//crumb)" \
  --user "$JENKINS_USER:$JENKINS_API_TOKEN")
curl -X POST "$JENKINS_URL/job/target-app/view/change-requests/job/PR-7/build" \
  --user "$JENKINS_USER:$JENKINS_API_TOKEN" -H "$CRUMB"
```

## Authentication

All trigger endpoints require a valid Jenkins API token. Set `JENKINS_USER` and
`JENKINS_API_TOKEN` environment variables.

## Feedback Loop Bounds

Reinvestigation is controlled by **two separate caps** to prevent runaway loops:

| Variable | Default | Scope | Effect |
|---|---|---|---|
| `MAX_ATTEMPTS` | **2** | Backend (`validation.py`) | Max number of `incident.reinvestigate` events the backend publishes before escalating to human review |
| `REINVESTIGATION_MAX_ATTEMPTS` | **5** | Jenkins Agent (`agent.py`) | Hard cap on LLM calls; the agent raises `ValueError` if exceeded — a safety net against misconfiguration |

Set `MAX_ATTEMPTS=3` (or higher) in the backend if two validation rounds are
insufficient for your workflow. The agent-side cap (`REINVESTIGATION_MAX_ATTEMPTS`)
is a last-resort guard and should always be ≥ `MAX_ATTEMPTS`.

## Verifying the Trigger URL in Your Instance

The exact build-trigger path differs between Jenkins versions, SCM plugins, and
Multibranch configurations. Use the following commands to discover the working
URL for your instance before wiring it up in production.

**Step 1 – List jobs inside the pipeline:**

```bash
curl -s \
  "$JENKINS_URL/job/target-app/api/json?tree=jobs[name,url]" \
  --user "$JENKINS_USER:$JENKINS_API_TOKEN" | python3 -m json.tool
```

**Step 2 – Find PR / change-request views:**

```bash
curl -s \
  "$JENKINS_URL/job/target-app/api/json?tree=views[name,url]" \
  --user "$JENKINS_USER:$JENKINS_API_TOKEN" | python3 -m json.tool
```

**Step 3 – List PR jobs within the change-requests view:**

```bash
curl -s \
  "$JENKINS_URL/job/target-app/view/change-requests/api/json?tree=jobs[name,url]" \
  --user "$JENKINS_USER:$JENKINS_API_TOKEN" | python3 -m json.tool
```

**Step 4 – Dry-run a trigger (use `-v` to see the response code):**

```bash
curl -v -X POST \
  "$JENKINS_URL/job/target-app/view/change-requests/job/PR-7/build" \
  --user "$JENKINS_USER:$JENKINS_API_TOKEN"
```

A `201 Created` or `200 OK` response confirms the URL is correct. Record the
working URL in this file before enabling automatic PR builds.

## Explicit Trigger via `trigger_build_url()`

`JenkinsClient.trigger_build_url(build_trigger_url)` can be called to bypass
the Multibranch scan delay (up to **2 minutes**) and immediately queue a PR
build using a pre-discovered URL:

```python
from agents.jenkins_agent.tools.jenkins_client import JenkinsClient

async with JenkinsClient() as client:
    await client.trigger_build_url(
        "http://jenkins:8080/job/target-app/view/change-requests/job/PR-7/build"
    )
```

Both `200 OK` and `201 Created` are accepted as success. The method raises:

- `JenkinsNotFound` — if the URL returns HTTP 404 (wrong path)
- `JenkinsAuthError` — if the URL returns HTTP 401 (bad credentials)
- `JenkinsUnavailable` — on 5xx or transport errors after exhausting retries

Use this instead of `trigger_build(job)` when the branch path is non-standard
(e.g. a GitHub PR viewed through the `change-requests` view).

## Edge Cases

| Scenario | Behavior |
|---|---|
| Multibranch scan delay (up to 2 min) | Use `trigger_build_url()` for immediate trigger; do not wait |
| PR build path varies | Verify with `curl` steps above; record in env var |
| Build still running when reinvestigation fires | Agent records `insufficient_evidence` and defers; Coordinator re-queues |
| Fix deletes tests (test count drops) | Agent flags `WARNING` in Evidence; OPA `remediation.rego` blocks auto-approval with `deny` |
| Flaky failure at same stage | Flaky tool is re-run on the new build; if `flaky_score > 0.4`, evidence is `FLAKY_TEST` |
| Same error signature after fix | Agent reports "same_signature: fix didn't address root cause" |
| Different error signature after fix | Agent reports "new_signature: fix introduced or exposed new problem" |
| `MAX_ATTEMPTS` exceeded | Backend escalates incident to `ESCALATED` for human review; agent raises `ValueError` |
