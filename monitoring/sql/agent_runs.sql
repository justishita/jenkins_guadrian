-- agent_runs: one row per agent run, computed from audit_events (nothing writes the agent_runs table).
--
-- A run is an `agent_started` paired with the next terminal event (`agent_completed` or
-- `agent_failed`) for the same (incident, actor). Two starts with no terminal between them leave the
-- first one "running" (unfinished). Redelivery therefore gives two rows, as it should.
--
-- Agents that write no audit of their own (the Jenkins agent) are covered by the Coordinator's
-- `agent_dispatch` event: one row per dispatch with the outcome and duration, `started_at` NULL
-- (the event is written when the dispatch ends). An agent that has run events for the incident is
-- not double-counted by its dispatch row.
--
-- Used as a CTE by the Agent Ops dashboard:  WITH agent_runs AS ( <this file> ) SELECT ... FROM agent_runs
-- Portable on purpose (window functions + JSON ->> only; NULLs are explicitly typed because PostgreSQL
-- refuses to UNION an untyped NULL column with a timestamp) so it is unit-tested against SQLite and runs on PostgreSQL.
WITH ev AS (
    SELECT
        id, incident_id, actor, event_type, created_at, duration_ms, ok, payload,
        lag(event_type) OVER w AS prev_type,
        lag(created_at) OVER w AS prev_at,
        lead(event_type) OVER w AS next_type
    FROM audit_events
    WHERE event_type IN ('agent_started', 'agent_completed', 'agent_failed')
    WINDOW w AS (PARTITION BY incident_id, actor ORDER BY created_at, id)
),
finished AS (
    SELECT
        incident_id,
        actor AS agent,
        prev_at AS started_at,
        created_at AS finished_at,
        COALESCE(payload ->> 'status', CASE WHEN event_type = 'agent_failed' THEN 'failed' ELSE 'completed' END) AS status,
        payload ->> 'failure_type' AS failure_type,
        CAST(payload ->> 'confidence' AS DOUBLE PRECISION) AS confidence,
        duration_ms,
        ok,
        'run' AS source
    FROM ev
    WHERE event_type IN ('agent_completed', 'agent_failed') AND prev_type = 'agent_started'
),
unfinished AS (
    SELECT
        incident_id,
        actor AS agent,
        created_at AS started_at,
        CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS finished_at,
        'running' AS status,
        CAST(NULL AS TEXT) AS failure_type,
        CAST(NULL AS DOUBLE PRECISION) AS confidence,
        CAST(NULL AS DOUBLE PRECISION) AS duration_ms,
        CAST(NULL AS BOOLEAN) AS ok,
        'run' AS source
    FROM ev
    WHERE event_type = 'agent_started' AND (next_type IS NULL OR next_type = 'agent_started')
),
dispatched AS (
    SELECT
        d.incident_id,
        d.payload ->> 'agent' AS agent,
        CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS started_at,
        d.created_at AS finished_at,
        d.payload ->> 'outcome' AS status,
        CAST(NULL AS TEXT) AS failure_type,
        CAST(NULL AS DOUBLE PRECISION) AS confidence,
        d.duration_ms,
        d.ok,
        'dispatch' AS source
    FROM audit_events d
    WHERE d.event_type = 'agent_dispatch'
      AND NOT EXISTS (
          SELECT 1 FROM audit_events r
          WHERE r.incident_id = d.incident_id
            AND r.actor = d.payload ->> 'agent'
            AND r.event_type = 'agent_started'
      )
)
SELECT * FROM finished
UNION ALL
SELECT * FROM unfinished
UNION ALL
SELECT * FROM dispatched
