# Substitute Draft-PR Workflow Specification

## Overview

Until P3 delivers their final draft-PR automation, this document and its accompanying implementations (`.github/workflows/draft-pr.yml` and `scripts/draft_pr_workflow.py`) serve as the official substitute draft-PR workflow for JenkinsGuardian.

## Branch Naming Contract

All remediation branches **MUST** follow the naming format:

```
ai-fix/<incident_id>-<attempt>
```

- `incident_id`: The canonical 36-character UUID of the incident.
- `attempt`: A non-negative integer (0 for first fix attempt, 1 for second, etc.).

### Automatic Validation Linking
When Jenkins builds the draft PR, the Jenkins validation webhook (`backend/routes/validation.py`) extracts the branch name from the webhook payload:
```python
clean_branch = payload.branch.replace("refs/heads/", "").replace("origin/", "").strip()
if "ai-fix/" in clean_branch:
    sub = clean_branch.split("ai-fix/", 1)[1]
    match = re.match(r"^(.*?)(?:-(\d+))?$", sub)
    if match:
        resolved_id = match.group(1).strip()
        resolved_attempt = int(match.group(2))
```
This allows the backend to automatically correlate the validation build back to the exact incident and attempt without needing database lookups.

## Gates Before Draft PR Creation

1. **Remediation Policy Check (OPA)**:
   - Evaluates `policies/remediation.rego`.
   - Confirms no test files or safety checks were removed or skipped.
   - Ensures diff size and changed files are minimal (≤ 5 files, ≤ 40 lines).
2. **Human Approval Gate**:
   - Recorded named human approval is required (`approved`, `rejected`, `changes_requested`).
3. **Pull Request Policy (OPA)**:
   - Evaluates `policies/pull_request.rego`.
   - Strictly enforces `draft: true` and `auto_merge: false`.

## Workflow Triggering

### Via GitHub Actions
Pushing to any branch matching `ai-fix/**` triggers `.github/workflows/draft-pr.yml`, which:
1. Validates branch name structure.
2. Runs OPA policy verification.
3. Opens a Draft PR against `main`.
4. Dispatches the build trigger to Jenkins.

### Via CLI Script
```bash
python scripts/draft_pr_workflow.py create \
  --incident-id "11111111-2222-3333-4444-555555555555" \
  --attempt 1 \
  --base-ref "abc123" \
  --json
```
