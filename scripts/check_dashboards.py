"""Run every Grafana panel of the generated dashboards against the running stack and report what it shows.

**Owner:** P2 (Week 8, dashboard polish). A dashboard that renders is not a dashboard that works: a query can fail only
for one project filter, or only for the "All" list, or return nothing because an event is never written. This sends each
panel's query through Grafana's own query API (the way the browser does, with the dashboard variables filled in the way
Grafana fills them) for every project filter, and classifies each result:

    ok      the query ran and returned data
    empty   the query ran and returned no rows (or only nulls): normal for an approval panel before any approval,
            a finding for a panel that should have data
    ERROR   the query failed: always a bug

    python scripts/check_dashboards.py                 # all dashboards, last 30 days, every project + "All"
    python scripts/check_dashboards.py --range 24h
    python scripts/check_dashboards.py --dashboard ops

Needs the stack up (``docker compose up -d``); it talks to Grafana inside its container, so no credentials are used. Exits 1
if any panel errors, 0 otherwise: empty panels are reported, not failed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DASHBOARDS = REPO / "monitoring" / "grafana" / "dashboards"
GRAFANA_QUERY = "http://localhost:3000/grafana/api/ds/query"
NO_INCIDENT = "00000000-0000-0000-0000-000000000000"
RANGES = {"24h": "now-24h", "7d": "now-7d", "30d": "now-30d", "90d": "now-90d"}


@dataclass
class Result:
    dashboard: str
    panel: str
    filter: str
    state: str  # ok | empty | ERROR
    rows: int = 0
    error: str = ""


def sql_literal(value: str) -> str:
    """What Grafana's Postgres datasource substitutes for a quoted variable value (escaped, single-quoted)."""
    return "'" + value.replace("'", "''") + "'"


def filters(projects: list[str]) -> dict[str, str]:
    """Variable shapes the dashboards must survive: every project's list (what "All" expands to) and each project alone."""
    shapes = {"All": ",".join(sql_literal(p) for p in projects) or "''"}
    for project in projects:
        shapes[project] = sql_literal(project)
    return shapes


def interpolate(sql: str, job: str, incident: str) -> str:
    return sql.replace("$job", job).replace("$incident", incident)


def classify(response: dict[str, Any]) -> tuple[str, int, str]:
    """(state, rows, error) from a /api/ds/query response for one query (refId A)."""
    result = (response.get("results") or {}).get("A") or {}
    if result.get("error") or (result.get("status") not in (None, 200)):
        return "ERROR", 0, str(result.get("error") or result.get("status"))[:200]
    rows = 0
    for frame in result.get("frames") or []:
        columns = (frame.get("data") or {}).get("values") or []
        if not columns:
            continue
        # a frame whose every cell is null (e.g. an average over nothing) is empty, not data
        rows += sum(1 for index in range(len(columns[0])) if any(column[index] is not None for column in columns))
    return ("ok" if rows else "empty"), rows, ""


def grafana_query(body: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "grafana", "curl", "-s", "-X", "POST", "-H", "Content-Type: application/json",
         "-H", "X-WEBAUTH-USER: ui-viewer", GRAFANA_QUERY, "-d", json.dumps(body)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"Grafana did not answer ({result.stderr.strip()[:200] or 'empty reply'}); is the stack up? docker compose up -d")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(f"Grafana returned something that is not JSON: {result.stdout[:200]}") from error


def panel_body(panel: dict[str, Any], job: str, incident: str, start: str) -> dict[str, Any]:
    target = panel["targets"][0]
    if "rawSql" in target:
        query = {"refId": "A", "datasource": panel["datasource"], "format": target.get("format", "table"), "rawQuery": True, "editorMode": "code",
                 "rawSql": interpolate(target["rawSql"], job, incident), "intervalMs": 60000, "maxDataPoints": 500}
    else:  # a Prometheus panel
        query = {"refId": "A", "datasource": panel["datasource"], "expr": target["expr"], "instant": True}
    return {"queries": [query], "from": start, "to": "now"}


def scalar(sql: str) -> list[str]:
    body = {"queries": [{"refId": "A", "datasource": {"type": "grafana-postgresql-datasource", "uid": "postgres"}, "format": "table",
                         "rawQuery": True, "editorMode": "code", "rawSql": sql}], "from": "now-1h", "to": "now"}
    frames = (grafana_query(body).get("results", {}).get("A", {}).get("frames") or [])
    return [str(value) for frame in frames for value in ((frame.get("data") or {}).get("values") or [[]])[0]]


def check(dashboard_names: list[str], start: str) -> list[Result]:
    projects = scalar("SELECT jenkins_job FROM projects WHERE NOT archived ORDER BY jenkins_job")
    incidents = scalar("SELECT id FROM incidents ORDER BY created_at DESC LIMIT 1")
    incident = incidents[0] if incidents else NO_INCIDENT
    results: list[Result] = []
    for name in dashboard_names:
        document = json.loads((DASHBOARDS / f"{name}.json").read_text(encoding="utf-8"))
        for panel in document["panels"]:
            for label, job in filters(projects).items():
                state, rows, error = classify(grafana_query(panel_body(panel, job, incident, start)))
                results.append(Result(name, panel["title"], label, state, rows, error))
    return results


def summarize(results: list[Result]) -> dict[str, Any]:
    by_state = Counter(r.state for r in results)
    panels: dict[tuple[str, str], list[Result]] = {}
    for r in results:
        panels.setdefault((r.dashboard, r.panel), []).append(r)
    empty_everywhere = sorted(key for key, rs in panels.items() if all(r.state == "empty" for r in rs))
    errors = [r for r in results if r.state == "ERROR"]
    return {"checks": len(results), "panels": len(panels), "by_state": dict(by_state), "empty_everywhere": empty_everywhere, "errors": errors}


def render(summary: dict[str, Any], start: str) -> str:
    lines = [f"Checked {summary['panels']} panels x project filters = {summary['checks']} queries (range {start} .. now): "
             + ", ".join(f"{count} {state}" for state, count in sorted(summary["by_state"].items()))]
    if summary["errors"]:
        lines.append("\nERRORS (always a bug):")
        lines += [f"  {r.dashboard} / {r.panel} [{r.filter}]: {r.error}" for r in summary["errors"]]
    if summary["empty_everywhere"]:
        lines.append("\nEmpty under every filter (expected only when the event never happened yet):")
        lines += [f"  {dashboard} / {panel}" for dashboard, panel in summary["empty_everywhere"]]
    if not summary["errors"]:
        lines.append("\nNo panel errors.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dashboard", choices=["agent_ops", "ops"], action="append", help="default: both")
    parser.add_argument("--range", choices=sorted(RANGES), default="30d")
    args = parser.parse_args(argv)
    start = RANGES[args.range]
    try:
        summary = summarize(check(args.dashboard or ["agent_ops", "ops"], start))
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(error)
        return 2
    print(render(summary, start))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
