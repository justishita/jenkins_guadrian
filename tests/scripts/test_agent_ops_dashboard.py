"""The Agent Ops dashboard is generated, reads only the audit tables, and has the required panels."""

import json
import re
import runpy
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = REPO / "monitoring" / "grafana" / "dashboards" / "agent_ops.json"


def generated() -> dict:
    return runpy.run_path(str(REPO / "scripts" / "generate_agent_ops_dashboard.py"))["dashboard"]


def panels() -> dict[str, dict]:
    return {p["title"]: p for p in generated()["panels"]}


def sql(panel: dict) -> str:
    return panel["targets"][0]["rawSql"]


def test_checked_in_dashboard_matches_the_generator() -> None:
    assert json.loads(DASHBOARD.read_text(encoding="utf-8")) == generated()


def test_panel_ids_and_titles_are_unique() -> None:
    items = generated()["panels"]
    assert len({p["id"] for p in items}) == len(items) == len({p["title"] for p in items})


def test_has_the_agent_duration_heat_map_over_all_agents() -> None:
    heatmap = panels()["Agent run duration (heat map)"]
    assert heatmap["type"] == "heatmap" and heatmap["options"]["calculate"] is True
    assert "agent_runs" in sql(heatmap)


def test_covers_investigation_time_approval_rate_and_the_accuracy_proxy() -> None:
    titles = panels()
    assert {"Investigation time by agent", "Time to report", "Approval rate", "Fix success rate (accuracy proxy)"} <= set(titles)
    assert "approval_decided" in sql(titles["Approval rate"]) and "'approved'" in sql(titles["Approval rate"])
    assert "revalidation_completed" in sql(titles["Fix success rate (accuracy proxy)"])
    # The proxy is labelled as one; no panel claims verdict accuracy it cannot measure.
    assert "proxy" in titles["Fix success rate (accuracy proxy)"]["title"]
    assert not any(re.search(r"\baccuracy\b", t) and "proxy" not in t for t in titles)


def test_per_run_panels_embed_the_agent_runs_query() -> None:
    from_file = "\n".join(
        line for line in (REPO / "monitoring" / "sql" / "agent_runs.sql").read_text(encoding="utf-8").splitlines() if not line.startswith("--")
    ).strip()
    for title, panel in panels().items():
        if "FROM agent_runs" in sql(panel):
            assert sql(panel).startswith("WITH agent_runs AS ("), title
            assert from_file in sql(panel), title


def test_every_panel_reads_the_provisioned_postgres_datasource_over_the_audit_tables() -> None:
    for panel in generated()["panels"]:
        assert panel["datasource"]["uid"] == "postgres"
        assert "audit_events" in sql(panel), panel["title"]


def test_time_filtered_panels_respect_the_dashboard_time_range() -> None:
    for title, panel in panels().items():
        if title.startswith("Incident timeline"):
            continue  # one chosen incident, not a time range
        assert "$__timeFilter(" in sql(panel), title


def test_the_incident_timeline_uses_the_incident_variable() -> None:
    dashboard = generated()
    timeline = next(p for p in dashboard["panels"] if p["title"].startswith("Incident timeline"))
    assert "'$incident'" in sql(timeline)
    [variable] = dashboard["templating"]["list"]
    assert variable["name"] == "incident" and "audit_events" in variable["query"]


def test_panels_are_read_only_selects() -> None:
    forbidden = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|grant)\b", re.IGNORECASE)
    for title, panel in panels().items():
        statement = sql(panel)
        assert statement.lstrip().upper().startswith(("SELECT", "WITH")), title
        assert not forbidden.search(statement), title


@pytest.mark.parametrize("panel", generated()["panels"], ids=lambda p: p["title"])
def test_panels_fit_the_24_column_grid(panel: dict) -> None:
    grid = panel["gridPos"]
    assert grid["x"] >= 0 and grid["w"] > 0 and grid["x"] + grid["w"] <= 24


def test_datasource_uid_matches_the_provisioning_file() -> None:
    text = (REPO / "monitoring" / "grafana" / "provisioning" / "datasources" / "postgres.yml").read_text(encoding="utf-8")
    assert "uid: postgres" in text and "${POSTGRES_PASSWORD}" in text
