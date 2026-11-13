# Backend Routes

**Owner:** P1. Owns the FastAPI HTTP routes, including the Jenkins webhook.
`approvals.py` is the exception: **owner P3**. It implements the shared approval
contract agreed between P1 (approval UI, notifications), P2 (approval dashboards) and
P3 (the `approvals` table):

| | |
| --- | --- |
| `GET /approvals/{incident_id}` | the fix awaiting a decision, its policy verdicts, the agents' evidence |
| `POST /approvals/{incident_id}` | `{decision: approved\|rejected, reviewer, comment}`; one decision per attempt |
| event `approval.decided` | `{incident_id, attempt, proposal_id, branch, decision, reviewer, comment, decided_at}` on the `incidents` exchange |
| table `approvals` | `incident_id, attempt, decision, reviewer, comment, decided_at` (migration `0002`) |

Both endpoints need `Authorization: Bearer $APPROVAL_API_TOKEN`; with it unset they
refuse everything. Responses: `201` recorded, `404` unknown incident, `409` nothing
awaiting a decision (no proposal, blocked by policy, or already decided), `422`
malformed body.

`remediations.py` is also **owner P3**: the live half of the same contract, called by
the Coordinator (same bearer token).

| | |
| --- | --- |
| `POST /remediations` | the Coordinator registers a proposed fix and its policy verdict; if allowed, `approval.requested` is published so the notifier asks a reviewer. Idempotent per `(incident_id, attempt)` |
| `PATCH /remediations/{incident_id}/{attempt}` | after a decision, the Coordinator reports the draft PR it opened (`pr_created` + URL) or why none was (`pr_blocked`, `failed`) |
