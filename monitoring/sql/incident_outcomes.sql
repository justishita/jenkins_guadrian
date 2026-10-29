-- incident_outcomes: one row per incident with how its investigation ended (or where it is now).
--
-- Derived, not stored: from `incidents` (current status), `audit_events` (status changes, decisions,
-- reports, re-validations) and, for fixes waiting on a human, `remediation_proposals`/`approvals`.
--
-- outcome:
--   resolved_by_fix    status RESOLVED (a fix was approved and Jenkins re-validation passed)
--   rejected           a human rejected the fix (status CLOSED_REJECTED, or a rejection recorded before that status existed)
--   escalated          status ESCALATED (re-validation kept failing until the attempts ran out)
--   awaiting_validation the fix was approved; Jenkins re-validation has not finished
--   awaiting_approval  a proposed fix is waiting for a human decision
--   policy_blocked     the only proposal was refused by policy
--   open               still being investigated, or no fix was proposed
--
-- closed_at is when the incident reached RESOLVED / ESCALATED / CLOSED_REJECTED (the status-change audit
-- event; for older RESOLVED incidents, the passing re-validation). Durations are left to the caller because
-- timestamp arithmetic is not portable (PostgreSQL extract(epoch ...)); the view works on SQLite for the tests.
--
-- Used as a CTE by the dashboards:  WITH incident_outcomes AS ( <this file> ) SELECT ... FROM incident_outcomes
WITH base AS (
    SELECT
        i.id AS incident_id,
        i.job_name,
        i.status,
        i.remediation_attempt AS attempt,
        i.created_at AS opened_at,
        (SELECT a.payload ->> 'verdict' FROM audit_events a
          WHERE a.incident_id = i.id AND a.event_type = 'synthesis_completed'
          ORDER BY a.created_at DESC, a.id DESC LIMIT 1) AS verdict,
        (SELECT a.payload ->> 'decision' FROM audit_events a
          WHERE a.incident_id = i.id AND a.event_type = 'approval_decided'
          ORDER BY a.created_at DESC, a.id DESC LIMIT 1) AS decision,
        (SELECT a.payload ->> 'reviewer' FROM audit_events a
          WHERE a.incident_id = i.id AND a.event_type = 'approval_decided'
          ORDER BY a.created_at DESC, a.id DESC LIMIT 1) AS approver,
        (SELECT count(*) FROM audit_events a
          WHERE a.incident_id = i.id AND a.event_type = 'revalidation_completed') AS revalidations,
        (SELECT max(a.created_at) FROM audit_events a
          WHERE a.incident_id = i.id AND a.event_type = 'incident_status_changed'
            AND (a.payload ->> 'to') IN ('RESOLVED', 'ESCALATED', 'CLOSED_REJECTED')) AS status_closed_at,
        (SELECT max(a.created_at) FROM audit_events a
          WHERE a.incident_id = i.id AND a.event_type = 'revalidation_completed' AND a.ok) AS last_passed_at,
        EXISTS (SELECT 1 FROM remediation_proposals p
                 WHERE p.incident_id = i.id AND p.status = 'awaiting_approval'
                   AND NOT EXISTS (SELECT 1 FROM approvals ap WHERE ap.incident_id = p.incident_id AND ap.attempt = p.attempt)) AS awaiting,
        EXISTS (SELECT 1 FROM remediation_proposals p
                 WHERE p.incident_id = i.id AND p.status = 'policy_rejected') AS policy_blocked
    FROM incidents i
)
SELECT
    incident_id,
    job_name,
    status,
    attempt,
    opened_at,
    verdict,
    decision,
    approver,
    revalidations,
    CASE
        WHEN status = 'RESOLVED' THEN 'resolved_by_fix'
        WHEN status = 'ESCALATED' THEN 'escalated'
        WHEN status = 'CLOSED_REJECTED' THEN 'rejected'
        WHEN decision = 'rejected' THEN 'rejected'
        WHEN decision = 'approved' THEN 'awaiting_validation'
        WHEN awaiting THEN 'awaiting_approval'
        WHEN policy_blocked THEN 'policy_blocked'
        ELSE 'open'
    END AS outcome,
    CASE
        WHEN status IN ('RESOLVED', 'ESCALATED', 'CLOSED_REJECTED') THEN COALESCE(status_closed_at, last_passed_at)
        ELSE NULL
    END AS closed_at
FROM base
