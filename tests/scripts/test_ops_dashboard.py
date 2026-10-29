"""The Approvals & Operations dashboard is generated, project-scoped, read-only and covers the Week 7 scope."""

import json
import re
import runpy
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "generate_agent_ops_dashboard.py"
DASHBOARD = REPO / "monitoring" / "grafana" / "dashboards" / "ops.json"


@pytest.fixture(scope="module")
def generated() -> dict:
    return runpy.run_path(str(SCRIPT))


@pytest.fixture(scope="module")
def dashboard(generated) -> dict:
    return generated["ops_dashboard"]


def panels(dashboard) -> dict[str, dict]:
    return {p["title"]: p for p in dashboard["panels"]}


def sql(panel: dict) -> str:
    return panel["targets"][0]["rawSql"]


def sql_panels(dashboard) -> dict[str, dict]:
    return {title: panel for title, panel in panels(dashboard).items() if panel["datasource"]["uid"] == "postgres"}


def test_the_checked_in_file_matches_the_generator(dashboard) -> None:
    assert json.loads(DASHBOARD.read_text(encoding="utf-8")) == dashboard


def test_it_has_its_own_uid_and_title(dashboard, generated) -> None:
    assert dashboard["uid"] == "ops" and dashboard["title"] == "Approvals & Operations"
    assert dashboard["uid"] != generated["dashboard"]["uid"]


def test_panel_ids_and_titles_are_unique(dashboard) -> None:
    items = dashboard["panels"]
    assert len({p["id"] for p in items}) == len(items) == len({p["title"] for p in items})


@pytest.mark.parametrize("title", [
    "Awaiting approval now", "Oldest waiting (minutes)", "Escalated (in range)", "Resolved by fix (in range)",
    "Fix rejected (in range)", "Median time to resolve (min)", "Incident outcomes", "Approval decisions over time",
    "Decisions by reviewer", "Approval latency", "Agent failures and timeouts over time", "Dispatch outcomes over time",
    "Re-validation pass rate", "Re-validations per incident", "Policy denial reasons", "Time between steps (median, min)",
    "Escalated and rejected incidents", "Service health (Prometheus up)",
])
def test_the_week_7_panels_exist(dashboard, title) -> None:
    assert title in panels(dashboard)


def test_every_audit_panel_is_limited_to_the_selected_project(dashboard) -> None:
    for title, panel in sql_panels(dashboard).items():
        assert "unnest(ARRAY[$job]::text[])" in sql(panel), title
        assert "i.job_name = p.job" in sql(panel) and "= p.job || '/'" in sql(panel), title


def test_the_job_variable_is_unquoted_and_has_no_custom_all_value(dashboard) -> None:
    for title, panel in sql_panels(dashboard).items():
        assert "'$job" not in sql(panel) and "$job'" not in sql(panel), title
    [variable] = dashboard["templating"]["list"]
    assert variable["name"] == "job" and variable["includeAll"] is True and "allValue" not in variable
    assert "FROM projects" in variable["query"]


def test_panels_follow_the_dashboard_time_range_except_the_current_state_ones(dashboard, generated) -> None:
    exempt = set(generated["OPS_CURRENT_STATE"])
    for title, panel in sql_panels(dashboard).items():
        if title in exempt:
            assert "$__timeFilter(" not in sql(panel), title  # a "now" panel must not shrink with the range
        else:
            assert "$__timeFilter(" in sql(panel), title


def test_outcome_panels_use_the_incident_outcomes_view_verbatim(dashboard) -> None:
    view = "\n".join(
        line for line in (REPO / "monitoring" / "sql" / "incident_outcomes.sql").read_text(encoding="utf-8").splitlines() if not line.startswith("--")
    ).strip()
    users = [t for t, p in sql_panels(dashboard).items() if "FROM incident_outcomes" in sql(p)]
    assert len(users) >= 6
    for title in users:
        assert sql(panels(dashboard)[title]).startswith("WITH incident_outcomes AS ("), title
        assert view in sql(panels(dashboard)[title]), title


def test_failure_panels_use_the_agent_runs_view(dashboard) -> None:
    panel = panels(dashboard)["Agent failures and timeouts over time"]
    assert sql(panel).startswith("WITH agent_runs AS (")


def test_panels_only_read_the_audit_tables(dashboard) -> None:
    allowed = {"audit_events", "incidents", "remediation_proposals", "approvals", "projects"}
    for title, panel in sql_panels(dashboard).items():
        tables = set(re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+)", sql(panel)))
        tables -= {"incident_outcomes", "agent_runs", "base", "steps", "unnest", "ev", "finished", "unfinished", "dispatched"}
        assert tables <= allowed, (title, tables - allowed)


def test_panels_are_read_only_selects(dashboard) -> None:
    forbidden = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|grant)\b", re.IGNORECASE)
    for title, panel in sql_panels(dashboard).items():
        assert sql(panel).lstrip().upper().startswith(("SELECT", "WITH")), title
        assert not forbidden.search(sql(panel)), title


def test_service_health_reads_prometheus_up(dashboard) -> None:
    panel = panels(dashboard)["Service health (Prometheus up)"]
    assert panel["datasource"]["uid"] == "prometheus" and panel["targets"][0]["expr"] == "up"


@pytest.mark.parametrize("index", range(18))
def test_panels_fit_the_24_column_grid(dashboard, index) -> None:
    grid = dashboard["panels"][index]["gridPos"]
    assert grid["x"] >= 0 and grid["w"] > 0 and grid["x"] + grid["w"] <= 24


def test_panels_do_not_overlap(dashboard) -> None:
    cells: dict[tuple[int, int], str] = {}
    for panel in dashboard["panels"]:
        grid = panel["gridPos"]
        for x in range(grid["x"], grid["x"] + grid["w"]):
            for y in range(grid["y"], grid["y"] + grid["h"]):
                assert (x, y) not in cells, f"{panel['title']} overlaps {cells[(x, y)]}"
                cells[(x, y)] = panel["title"]
