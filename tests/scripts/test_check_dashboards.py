"""scripts/check_dashboards.py: variable filling, result classification and the summary (no stack needed)."""

import runpy
from pathlib import Path

SCRIPT = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "check_dashboards.py"))
sql_literal, filters, interpolate, classify = SCRIPT["sql_literal"], SCRIPT["filters"], SCRIPT["interpolate"], SCRIPT["classify"]
summarize, render, Result, panel_body = SCRIPT["summarize"], SCRIPT["render"], SCRIPT["Result"], SCRIPT["panel_body"]
DASHBOARDS = SCRIPT["DASHBOARDS"]


def reply(*columns, status=200, error=None):
    result = {"status": status, "frames": [{"data": {"values": list(columns)}}]}
    if error:
        result["error"] = error
    return {"results": {"A": result}}


def test_values_are_quoted_and_escaped_the_way_grafana_does_it():
    assert sql_literal("target-app") == "'target-app'"
    assert sql_literal("x' OR 1=1 --") == "'x'' OR 1=1 --'"  # a hostile value stays one string


def test_all_expands_to_every_project_and_each_project_is_also_tried_alone():
    shapes = filters(["a", "b"])
    assert shapes == {"All": "'a','b'", "a": "'a'", "b": "'b'"}
    assert filters([]) == {"All": "''"}  # no projects registered: still a valid list


def test_both_variables_are_filled_into_the_query():
    assert interpolate("... ARRAY[$job] ... '$incident'", "'a'", "ID") == "... ARRAY['a'] ... 'ID'"


def test_a_query_with_rows_is_ok_and_one_without_is_empty():
    assert classify(reply([1, 2]))[:2] == ("ok", 2)
    assert classify(reply([]))[:2] == ("empty", 0)
    assert classify({"results": {"A": {"status": 200, "frames": []}}})[:2] == ("empty", 0)


def test_rows_of_only_nulls_are_empty_because_the_panel_would_show_no_data():
    assert classify(reply([None], [None]))[:2] == ("empty", 0)  # e.g. an average over nothing
    assert classify(reply([None, 3], [None, None]))[:2] == ("ok", 1)


def test_a_failed_query_is_an_error_with_the_message():
    state, rows, message = classify(reply(error='column "x" does not exist', status=500))
    assert state == "ERROR" and rows == 0 and "does not exist" in message
    assert classify({"results": {"A": {"status": 400}}})[0] == "ERROR"


def test_the_summary_separates_errors_from_panels_that_are_empty_everywhere():
    results = [
        Result("ops", "Approvals", "All", "empty"), Result("ops", "Approvals", "a", "empty"),
        Result("ops", "Outcomes", "All", "ok", 3), Result("ops", "Outcomes", "a", "empty"),
        Result("ops", "Broken", "All", "ERROR", error="boom"),
    ]
    summary = summarize(results)
    assert summary["by_state"] == {"empty": 3, "ok": 1, "ERROR": 1}
    assert summary["empty_everywhere"] == [("ops", "Approvals")]  # Outcomes has data under one filter
    assert [r.panel for r in summary["errors"]] == ["Broken"]
    text = render(summary, "now-30d")
    assert "ERRORS (always a bug)" in text and "ops / Broken [All]: boom" in text and "ops / Approvals" in text


def test_a_clean_run_says_so():
    assert "No panel errors." in render(summarize([Result("ops", "P", "All", "ok", 1)]), "now-24h")


def test_a_prometheus_panel_is_sent_as_an_instant_expression_and_a_sql_panel_as_raw_sql():
    prom = panel_body({"datasource": {"uid": "prometheus"}, "targets": [{"expr": "up"}]}, "'a'", "ID", "now-1h")
    assert prom["queries"][0]["expr"] == "up" and prom["queries"][0]["instant"] is True
    sql = panel_body({"datasource": {"uid": "postgres"}, "targets": [{"rawSql": "SELECT $job", "format": "table"}]}, "'a'", "ID", "now-1h")
    assert sql["queries"][0]["rawSql"] == "SELECT 'a'" and sql["from"] == "now-1h"


def test_both_generated_dashboards_exist_for_the_checker_to_read():
    assert (DASHBOARDS / "agent_ops.json").is_file() and (DASHBOARDS / "ops.json").is_file()
