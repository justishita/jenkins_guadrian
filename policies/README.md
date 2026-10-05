# Policy Guardrails

**Owner:** P3. These Rego policies decide whether an AI-proposed change is allowed to
exist, and whether it may become a pull request. They are the mechanism behind the
project's safety claim, so they live as reviewable data rather than as conditionals
scattered through the agent.

Two packages, evaluated at two different moments:

| Package | Evaluated | Answers |
| --- | --- | --- |
| `jenkinsguardians.remediation` | After the Coordinator synthesises a fix, before a human sees it | Is this proposal safe and minimal enough to put in front of a reviewer? |
| `jenkinsguardians.pullrequest` | After a human decides, before a PR is opened | Is there a real approval, and is this a draft? |

Each package exposes `deny` (hard stops), `warn` (shown to the reviewer) and `allow`,
where `allow` is true only when `deny` is empty.

## What `remediation` refuses

* A proposal with **confidence below 0.5** — the agent must keep investigating rather
  than guess (TC-14).
* A **non-minimal diff**: more than 5 files or more than 40 changed lines.
* Any change to a **protected path** — `Jenkinsfile`, `.github/`, `jenkins/`,
  `monitoring/`, `migrations/`, `docker-compose.yml`, a `Dockerfile`, the `Makefile`,
  or `policies/` itself. The agent cannot edit the rules that constrain it.
* Any change to a **secret-bearing path** — `.env`, `*.pem`, `*.key`, `id_rsa*`,
  `secrets*`, `credentials*`.
* Anything **outside `target_app/`**, the application the pipeline builds. The agent
  repairs the app under test, not the system investigating it.
* **Deleting a test file**, or adding a line that **silences a test or a safety gate**
  — `@pytest.mark.skip`, `pytest.skip(`, `--no-verify`, `verify=False`,
  `continue-on-error: true` and similar. This is TC-15: the obvious "fix" that makes
  the symptom disappear is exactly what must be blocked.

Dependency-manifest edits and moderate confidence produce a `warn`, not a `deny` — a
reviewer should look harder, not be prevented from looking.

## What `pullrequest` refuses

No policy pass, no recorded approval, an approval that is a rejection, an approval
with no named reviewer, a non-draft PR, or auto-merge enabled. There is no input a
machine can supply on its own that satisfies this package.

## Running the policies

They are served by the `opa` container (`docker compose up opa`) on port 8181, with
`./policies` mounted read-only, and queried by `coordinator/policy.py`. If OPA cannot
be reached, evaluation raises `PolicyUnavailable` and the proposal is blocked — a gate
that fails open is not a gate.

```bash
make policy-test                       # opa test policies
opa fmt --write policies               # formatting
conftest test --policy policies --namespace jenkinsguardians.remediation proposal.json
docker compose exec opa opa test /policies   # without a local opa binary
```

## Input documents

`remediation` takes the proposal:

```json
{
  "incident_id": "11111111-2222-3333-4444-555555555555",
  "failure_type": "config_error",
  "confidence": 0.82,
  "attempt": 0,
  "changed_files": [
    {
      "path": "target_app/config/settings.yaml",
      "status": "modified",
      "additions": 1,
      "deletions": 1,
      "patch": "@@ -1,2 +1,2 @@\n-database_timeout_seconds: -5\n+database_timeout_seconds: 3"
    }
  ]
}
```

`pullrequest` takes the gate state:

```json
{
  "policy_allowed": true,
  "draft": true,
  "auto_merge": false,
  "approval": {"decision": "approved", "reviewer": "ishita"}
}
```

## Changing a rule

Tighten or add rules in `remediation.rego` / `pull_request.rego`, add a case to the
matching `*_test.rego`, and keep `test_minimal_fix_is_allowed` passing — a rule that
blocks everything is as broken as one that blocks nothing. Every verdict, including
the ones that pass, is recorded in `policy_decisions` for the audit trail.
