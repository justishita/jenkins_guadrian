# Analytics MCP Server

**Owner:** Person 1 (Jenkins Agent)

Exposes analytics tools via the Model Context Protocol (FastMCP), port 8082:

- `compute_signature(failed_stage, error_blocks)` - deterministic SHA-256 error signature
  (the Jenkins agent exposes it to the model as `compute_error_signature`).
- `check_flakiness(test_name, job)` - flakiness of a named test over recent Jenkins runs.
  Reads the last builds from Jenkins using the same analysis as the Jenkins agent
  (`agents/jenkins_agent/tools/flaky.py`, shipped in the image). Needs `JENKINS_URL`,
  `JENKINS_USER` and `JENKINS_API_TOKEN` (compose passes them from `.env`). If Jenkins is not
  configured or cannot be reached it answers "not flaky, no history" and the message says the
  history is unavailable.
- `find_related_incidents(error_signature, job_name, window_days, current_incident_id)` -
  earlier incidents with the same error signature, found by scanning the incident evidence on
  the Blackboard. It reuses `agents/jenkins_agent/history.py`, the same lookup the Jenkins
  agent runs itself, so both give the same answer. Returns the matching incident ids (most
  recent first, at most 20), a similarity score per match (1.0: the match is exact), the
  recurrence count, and per-incident status, branch and creation time. `cross_branch` is
  true when a match was on another branch, and `regression_of_fixed_incident` when a match
  had already been resolved. The current incident is excluded and `window_days` is clamped
  to 1..365.

In Docker the server mounts `./data/blackboard` read-only at `/app/data/blackboard`
(`BLACKBOARD_DIR`).
