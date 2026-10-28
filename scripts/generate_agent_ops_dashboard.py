"""Generate monitoring/grafana/dashboards/agent_ops.json (the Agent Ops dashboard).

**Owner:** P2. Every panel reads the ``audit_events`` table (and ``incidents`` for time-to-report), so
it shows data only when the agents and the Coordinator run with ``DATABASE_URL`` set (the file audit
trail is not queryable from Grafana). The ``$job`` variable lists the projects registered in the UI and
limits every panel to that project's incidents; "All" shows everything. Re-run after editing a query::

    python scripts/generate_agent_ops_dashboard.py

Per-run numbers come from ``monitoring/sql/agent_runs.sql`` - a query over ``audit_events`` (nothing
writes the ``agent_runs`` table) that pairs each agent's start with its end and adds the Coordinator's
``agent_dispatch`` events for agents that write no audit of their own (the Jenkins agent).

"Accuracy" here is a **live proxy**, not verdict accuracy: live incidents carry no ground-truth label.
The proxy is the fix success rate - of the fixes that went through approval, how many passed Jenkins
re-validation. Labelled verdict accuracy belongs to the Week 8 evaluation.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "monitoring" / "grafana" / "dashboards" / "agent_ops.json"
AGENT_RUNS_SQL = "\n".join(
    line for line in (REPO / "monitoring" / "sql" / "agent_runs.sql").read_text(encoding="utf-8").splitlines() if not line.startswith("--")
).strip()
DS = {"type": "grafana-postgresql-datasource", "uid": "postgres"}

#: Prefix for panels that read per-run data.
RUNS = f"WITH agent_runs AS (\n{AGENT_RUNS_SQL}\n)\n"

_next_id = 0


def panel(kind: str, title: str, sql: str, x: int, y: int, w: int, h: int, *, fmt: str = "table", description: str = "", **extra):
    global _next_id
    _next_id += 1
    body = {
        "id": _next_id,
        "type": kind,
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"refId": "A", "datasource": DS, "format": fmt, "rawQuery": True, "editorMode": "code", "rawSql": sql}],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {},
    }
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(body.get(key), dict):
            body[key].update(value)
        else:
            body[key] = value
    return body


def thresholds(*steps: tuple[str, float | None]) -> dict:
    return {"defaults": {"thresholds": {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}}}


def scope(column: str = "incident_id") -> str:
    """Limit a panel to the project(s) picked in the ``$job`` variable.

    An incident belongs to a project when its job equals the project's Jenkins job or sits under it, the
    same rule the UI uses. ``$job`` is left unquoted on purpose: Grafana's SQL datasources quote and escape
    the value of a variable with an "All" option themselves, and expand "All" into the list of every
    project's job (``'a','b'``), so the condition takes a list. There is no custom All value: Grafana would
    insert that one unquoted.
    """
    return (
        f"{column} IN (SELECT i.id FROM incidents i WHERE EXISTS (SELECT 1 FROM unnest(ARRAY[$job]::text[]) AS p(job) "
        "WHERE i.job_name = p.job OR left(i.job_name, length(p.job) + 1) = p.job || '/'))"
    )


SYNTH = "event_type = 'synthesis_completed'"
BAD = "('failed', 'timed_out', 'not_dispatched')"
GREEN_BELOW_ONE = thresholds(("green", None), ("orange", 1))
PERCENT = {"defaults": {"unit": "percent", "min": 0, "max": 100, "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "orange", "value": 50}, {"color": "green", "value": 80}]}}}

panels = [
    # --- row 1: headline numbers ------------------------------------------------------------
    panel("stat", "Reports written",
          f"SELECT count(*) AS reports FROM audit_events WHERE {SYNTH} AND {scope()} AND $__timeFilter(created_at)",
          0, 0, 4, 4, description="Initial incident reports produced by the Coordinator in the selected range."),
    panel("stat", "Incomplete reports",
          f"SELECT count(*) AS incomplete FROM audit_events WHERE {SYNTH} AND payload->>'completeness' = 'incomplete' AND {scope()} AND $__timeFilter(created_at)",
          4, 0, 4, 4, description="Written on timeout or after an agent failed, was missing or never dispatched.", fieldConfig=GREEN_BELOW_ONE),
    panel("stat", "Conflicted reports",
          f"SELECT count(*) AS conflicted FROM audit_events WHERE {SYNTH} AND payload->>'conflict_status' = 'conflicted' AND {scope()} AND $__timeFilter(created_at)",
          8, 0, 4, 4, description="Top candidates too close to call.", fieldConfig=GREEN_BELOW_ONE),
    panel("stat", "Agent runs that failed or timed out",
          RUNS + f"SELECT count(*) AS problems FROM agent_runs WHERE status IN {BAD} AND {scope()} AND $__timeFilter(finished_at)",
          12, 0, 4, 4, description="failed, timed_out or not_dispatched runs, from the per-run view.",
          fieldConfig=thresholds(("green", None), ("red", 1))),
    panel("stat", "Approval rate",
          "SELECT round(100.0 * count(*) FILTER (WHERE payload->>'decision' = 'approved') / NULLIF(count(*), 0), 1) AS approval_rate "
          f"FROM audit_events WHERE event_type = 'approval_decided' AND {scope()} AND $__timeFilter(created_at)",
          16, 0, 4, 4, description="Share of human decisions that approved the proposed fix. Empty until a reviewer decides.", fieldConfig=PERCENT),
    panel("stat", "Fix success rate (accuracy proxy)",
          "SELECT round(100.0 * count(*) FILTER (WHERE ok) / NULLIF(count(*), 0), 1) AS fix_success FROM audit_events "
          f"WHERE event_type = 'revalidation_completed' AND {scope()} AND $__timeFilter(created_at)",
          20, 0, 4, 4,
          description="Fixes whose Jenkins re-validation passed. A live proxy: labelled verdict accuracy comes from the Week 8 evaluation.",
          fieldConfig=PERCENT),

    # --- row 2: what was concluded and decided ------------------------------------------------
    panel("barchart", "Verdicts",
          f"SELECT payload->>'verdict' AS verdict, count(*) AS reports FROM audit_events WHERE {SYNTH} AND {scope()} AND $__timeFilter(created_at) GROUP BY 1 ORDER BY 2 DESC",
          0, 4, 8, 7, description="What the Coordinator concluded, by failure type."),
    panel("barchart", "Approval decisions",
          "SELECT payload->>'decision' AS decision, count(*) AS decisions FROM audit_events WHERE event_type = 'approval_decided' "
          f"AND {scope()} AND $__timeFilter(created_at) GROUP BY 1 ORDER BY 2 DESC",
          8, 4, 8, 7, description="approved / rejected / changes_requested."),
    panel("barchart", "Fix pipeline outcomes",
          "SELECT stage, count(*) AS events FROM ("
          "SELECT 'proposed' AS stage, created_at, incident_id FROM audit_events WHERE event_type = 'remediation_proposed' "
          "UNION ALL SELECT 'policy denied', created_at, incident_id FROM audit_events WHERE event_type = 'policy_evaluated' AND (payload->>'allowed') = 'false' "
          "UNION ALL SELECT 'draft PR opened', created_at, incident_id FROM audit_events WHERE event_type = 'pull_request_created' AND ok "
          "UNION ALL SELECT 'revalidation passed', created_at, incident_id FROM audit_events WHERE event_type = 'revalidation_completed' AND ok "
          "UNION ALL SELECT 'revalidation failed', created_at, incident_id FROM audit_events WHERE event_type = 'revalidation_completed' AND NOT ok"
          f") s WHERE {scope()} AND $__timeFilter(created_at) GROUP BY 1 ORDER BY 2 DESC",
          16, 4, 8, 7, description="How far proposed fixes got: proposal, policy, pull request, Jenkins re-validation."),

    # --- investigation time --------------------------------------------------------------------
    panel("heatmap", "Agent run duration (heat map)",
          RUNS + "SELECT finished_at AS time, duration_ms / 1000.0 AS \"seconds\" FROM agent_runs "
          f"WHERE duration_ms IS NOT NULL AND {scope()} AND $__timeFilter(finished_at) ORDER BY 1",
          0, 11, 24, 9, fmt="time_series",
          description="How long investigations take, across all three agents (the Jenkins agent via the Coordinator's dispatch record). "
                      "Colour is the number of runs in a time/duration bucket.",
          options={
              "calculate": True,
              "calculation": {"yBuckets": {"mode": "count", "value": "12"}},
              "color": {"mode": "scheme", "scheme": "Oranges", "scale": "exponential", "exponent": 0.5, "fill": "dark-orange", "steps": 64},
              "yAxis": {"axisPlacement": "left", "unit": "s"},
              "tooltip": {"show": True, "yHistogram": True},
              "legend": {"show": True},
              "cellGap": 1,
          }),
    panel("table", "Investigation time by agent",
          RUNS + "SELECT agent, count(*) AS runs, count(*) FILTER (WHERE status IN " + BAD + ") AS problems, "
          "round((percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) / 1000)::numeric, 1) AS p50_s, "
          "round((percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) / 1000)::numeric, 1) AS p95_s "
          f"FROM agent_runs WHERE duration_ms IS NOT NULL AND {scope()} AND $__timeFilter(finished_at) GROUP BY agent ORDER BY agent",
          0, 20, 12, 8, description="Median and 95th-percentile run time per agent, with how many runs failed or timed out."),
    panel("table", "Time to report",
          "SELECT count(*) AS reports, "
          "round((percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM (a.created_at - i.created_at))))::numeric, 1) AS p50_s, "
          "round((percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM (a.created_at - i.created_at))))::numeric, 1) AS p95_s "
          f"FROM audit_events a JOIN incidents i ON i.id = a.incident_id WHERE a.event_type = 'synthesis_completed' AND {scope('a.incident_id')} AND $__timeFilter(a.created_at)",
          12, 20, 12, 8, description="Seconds from the incident being created to its initial report."),
    panel("timeseries", "Average run duration by agent",
          RUNS + "SELECT $__timeGroupAlias(finished_at, $__interval), agent AS metric, avg(duration_ms) / 1000.0 AS seconds "
          f"FROM agent_runs WHERE duration_ms IS NOT NULL AND {scope()} AND $__timeFilter(finished_at) GROUP BY 1, 2 ORDER BY 1",
          0, 28, 12, 8, fmt="time_series", fieldConfig={"defaults": {"unit": "s"}}),
    panel("timeseries", "Tool-call latency by agent",
          "SELECT $__timeGroupAlias(created_at, $__interval), actor AS metric, avg(duration_ms) AS ms "
          f"FROM audit_events WHERE event_type = 'tool_call' AND duration_ms IS NOT NULL AND {scope()} AND $__timeFilter(created_at) GROUP BY 1, 2 ORDER BY 1",
          12, 28, 12, 8, fmt="time_series", fieldConfig={"defaults": {"unit": "ms"}}),

    # --- approvals and policy ------------------------------------------------------------------
    panel("stat", "Average time to a decision",
          "SELECT round(avg(extract(epoch FROM (d.created_at - r.created_at)) / 60)::numeric, 1) AS minutes "
          "FROM audit_events d JOIN audit_events r ON r.incident_id = d.incident_id AND r.event_type = 'approval_requested' "
          f"WHERE d.event_type = 'approval_decided' AND d.created_at >= r.created_at AND {scope('d.incident_id')} AND $__timeFilter(d.created_at)",
          0, 36, 6, 7, description="Minutes from a fix being put in front of a reviewer to their decision.", fieldConfig={"defaults": {"unit": "m"}}),
    panel("table", "Recent approval decisions",
          "SELECT created_at AS \"time\", incident_id, payload->>'decision' AS decision, payload->>'reviewer' AS reviewer, "
          "payload->>'attempt' AS attempt, payload->>'comment' AS comment FROM audit_events "
          f"WHERE event_type = 'approval_decided' AND {scope()} AND $__timeFilter(created_at) ORDER BY created_at DESC LIMIT 20",
          6, 36, 18, 7),
    panel("table", "Policy denials",
          "SELECT created_at AS \"time\", incident_id, payload->>'package' AS package, payload->>'violations' AS violations "
          f"FROM audit_events WHERE event_type = 'policy_evaluated' AND (payload->>'allowed') = 'false' AND {scope()} AND $__timeFilter(created_at) "
          "ORDER BY created_at DESC LIMIT 20",
          0, 43, 24, 7, description="Proposals the policy layer refused, and why."),

    # --- reports and anomalies -----------------------------------------------------------------
    panel("table", "Recent reports",
          "SELECT created_at AS \"time\", incident_id, payload->>'verdict' AS verdict, (payload->>'confidence')::float AS score, "
          "payload->>'completeness' AS completeness, payload->>'conflict_status' AS conflict, payload->>'ready_for_policy_review' AS actionable "
          f"FROM audit_events WHERE {SYNTH} AND {scope()} AND $__timeFilter(created_at) ORDER BY created_at DESC LIMIT 20",
          0, 50, 16, 8, description="Score is the fused, uncalibrated value, not a probability. 'actionable' is what the remediation engine reads."),
    panel("barchart", "Hypotheses formed by agent",
          f"SELECT actor, count(*) AS hypotheses FROM audit_events WHERE event_type = 'hypothesis_formed' AND {scope()} AND $__timeFilter(created_at) GROUP BY 1 ORDER BY 2 DESC",
          16, 50, 8, 8, description="Anomaly findings: how many hypotheses each agent produced."),

    # --- one incident, end to end --------------------------------------------------------------
    panel("table", "Incident timeline (pick an incident above)",
          "SELECT created_at AS \"time\", actor, event_type, ok, round(duration_ms::numeric / 1000, 1) AS seconds, summary "
          "FROM audit_events WHERE incident_id = '$incident' ORDER BY created_at, id",
          0, 58, 24, 11, description="Every audited step for the selected incident, in order: agents, dispatch, report, policy, approval, re-validation."),
]

JOBS_QUERY = "SELECT jenkins_job FROM projects WHERE NOT archived ORDER BY jenkins_job"
INCIDENTS_QUERY = (
    "SELECT incident_id FROM audit_events WHERE incident_id <> '00000000-0000-0000-0000-000000000000' AND "
    + scope()
    + " GROUP BY incident_id ORDER BY max(created_at) DESC LIMIT 50"
)

dashboard = {
    "uid": "agent-ops",
    "title": "Agent Ops (anomaly + fusion)",
    "schemaVersion": 39,
    "version": 3,
    "refresh": "30s",
    "time": {"from": "now-24h", "to": "now"},
    "tags": ["jenkins-guardian"],
    "templating": {
        "list": [
            {
                "name": "job",
                "label": "Project (Jenkins job)",
                "type": "query",
                "datasource": DS,
                "query": JOBS_QUERY,
                "definition": JOBS_QUERY,
                "refresh": 2,
                "includeAll": True,
                "current": {"text": "All", "value": "$__all"},
                "multi": False,
                "sort": 1,
            },
            {
                "name": "incident",
                "label": "Incident",
                "type": "query",
                "datasource": DS,
                "query": INCIDENTS_QUERY,
                "definition": INCIDENTS_QUERY,
                "refresh": 2,
                "includeAll": False,
                "multi": False,
                "sort": 0,
            }
        ]
    },
    "panels": panels,
}

if __name__ == "__main__":
    OUT.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
