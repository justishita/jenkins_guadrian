"""The Agent Ops dashboard is generated, reads only the audit table, and has the heat map."""

import json
import runpy
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = REPO / "monitoring" / "grafana" / "dashboards" / "agent_ops.json"


def generated() -> dict:
    return runpy.run_path(str(REPO / "scripts" / "generate_agent_ops_dashboard.py"))["dashboard"]


def test_checked_in_dashboard_matches_the_generator() -> None:
    assert json.loads(DASHBOARD.read_text(encoding="utf-8")) == generated()


def test_has_the_agent_duration_heat_map() -> None:
    panels = {p["title"]: p for p in generated()["panels"]}
    heatmap = panels["Agent run duration (heat map)"]
    assert heatmap["type"] == "heatmap" and heatmap["options"]["calculate"] is True
    assert "agent_completed" in heatmap["targets"][0]["rawSql"]


def test_every_panel_queries_the_provisioned_postgres_datasource_over_audit_events() -> None:
    for panel in generated()["panels"]:
        assert panel["datasource"]["uid"] == "postgres"
        assert "audit_events" in panel["targets"][0]["rawSql"]
        assert "$__timeFilter(created_at)" in panel["targets"][0]["rawSql"], panel["title"]


def test_datasource_uid_matches_the_provisioning_file() -> None:
    text = (REPO / "monitoring" / "grafana" / "provisioning" / "datasources" / "postgres.yml").read_text(encoding="utf-8")
    assert "uid: postgres" in text and "${POSTGRES_PASSWORD}" in text
