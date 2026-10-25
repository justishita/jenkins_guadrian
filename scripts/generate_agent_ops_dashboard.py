"""Generate monitoring/grafana/dashboards/agent_ops.json (the Agent Ops dashboard).

**Owner:** P2. Every panel reads the ``audit_events`` table, so it shows data only when the
agents and the Coordinator run with ``DATABASE_URL`` set (the file audit trail is not
queryable from Grafana). Re-run after editing a query::

    python scripts/generate_agent_ops_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "monitoring" / "grafana" / "dashboards" / "agent_ops.json"
DS = {"type": "grafana-postgresql-datasource", "uid": "postgres"}

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


SYNTH = "event_type = 'synthesis_completed'"
panels = [
    panel(
        "stat", "Reports written", f"SELECT count(*) AS reports FROM audit_events WHERE {SYNTH} AND $__timeFilter(created_at)",
        0, 0, 4, 4, description="Unified incident reports produced by the Coordinator in the selected range.",
    ),
    panel(
        "stat", "Incomplete reports",
        f"SELECT count(*) AS incomplete FROM audit_events WHERE {SYNTH} AND payload->>'completeness' = 'incomplete' AND $__timeFilter(created_at)",
        4, 0, 4, 4, description="Written on timeout or after an agent failed.",
        fieldConfig={"defaults": {"thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 1}]}}},
    ),
    panel(
        "stat", "Conflicted reports",
        f"SELECT count(*) AS conflicted FROM audit_events WHERE {SYNTH} AND payload->>'conflict_status' = 'conflicted' AND $__timeFilter(created_at)",
        8, 0, 4, 4, description="Top candidates too close and not known to be compatible.",
        fieldConfig={"defaults": {"thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 1}]}}},
    ),
    panel(
        "stat", "Agent failures",
        "SELECT count(*) AS failures FROM audit_events WHERE event_type = 'agent_failed' AND $__timeFilter(created_at)",
        12, 0, 4, 4,
        fieldConfig={"defaults": {"thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 1}]}}},
    ),
    panel(
        "barchart", "Verdicts",
        f"SELECT payload->>'verdict' AS verdict, count(*) AS reports FROM audit_events WHERE {SYNTH} AND $__timeFilter(created_at) GROUP BY 1 ORDER BY 2 DESC",
        16, 0, 8, 4, description="What the Coordinator concluded, by failure type.",
    ),
    panel(
        "heatmap", "Agent run duration (heat map)",
        "SELECT created_at AS time, duration_ms / 1000.0 AS \"seconds\" FROM audit_events "
        "WHERE event_type = 'agent_completed' AND duration_ms IS NOT NULL AND $__timeFilter(created_at) ORDER BY 1",
        0, 4, 24, 9, fmt="time_series",
        description="How long investigations take, across all agents. Colour is the number of runs in a time/duration bucket.",
        options={
            "calculate": True,
            "calculation": {"yBuckets": {"mode": "count", "value": "12"}},
            "color": {"mode": "scheme", "scheme": "Oranges", "scale": "exponential", "exponent": 0.5, "fill": "dark-orange", "steps": 64},
            "yAxis": {"axisPlacement": "left", "unit": "s"},
            "tooltip": {"show": True, "yHistogram": True},
            "legend": {"show": True},
            "cellGap": 1,
        },
    ),
    panel(
        "timeseries", "Average run duration by agent",
        "SELECT $__timeGroupAlias(created_at, $__interval), actor AS metric, avg(duration_ms) / 1000.0 AS seconds "
        "FROM audit_events WHERE event_type = 'agent_completed' AND $__timeFilter(created_at) GROUP BY 1, 2 ORDER BY 1",
        0, 13, 12, 8, fmt="time_series", fieldConfig={"defaults": {"unit": "s"}},
    ),
    panel(
        "timeseries", "Tool-call latency by agent",
        "SELECT $__timeGroupAlias(created_at, $__interval), actor AS metric, avg(duration_ms) AS ms "
        "FROM audit_events WHERE event_type = 'tool_call' AND duration_ms IS NOT NULL AND $__timeFilter(created_at) GROUP BY 1, 2 ORDER BY 1",
        12, 13, 12, 8, fmt="time_series", fieldConfig={"defaults": {"unit": "ms"}},
    ),
    panel(
        "barchart", "Hypotheses formed by agent",
        "SELECT actor, count(*) AS hypotheses FROM audit_events WHERE event_type = 'hypothesis_formed' AND $__timeFilter(created_at) GROUP BY 1 ORDER BY 2 DESC",
        0, 21, 8, 8, description="Anomaly findings: how many hypotheses each agent produced.",
    ),
    panel(
        "table", "Recent reports",
        "SELECT created_at AS \"time\", incident_id, payload->>'verdict' AS verdict, "
        "(payload->>'confidence')::float AS score, payload->>'completeness' AS completeness, "
        "payload->>'conflict_status' AS conflict, payload->>'ready_for_policy_review' AS ready "
        f"FROM audit_events WHERE {SYNTH} AND $__timeFilter(created_at) ORDER BY created_at DESC LIMIT 20",
        8, 21, 16, 8, description="Score is the fused, uncalibrated value, not a probability.",
    ),
]

dashboard = {
    "uid": "agent-ops",
    "title": "Agent Ops (anomaly + fusion)",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "30s",
    "time": {"from": "now-24h", "to": "now"},
    "tags": ["jenkins-guardian"],
    "panels": panels,
}

if __name__ == "__main__":
    OUT.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
