"""The provisioned Grafana alert rules are well-formed, read-only and unambiguous."""

import re
from pathlib import Path

import pytest
import yaml

RULES = Path(__file__).resolve().parents[2] / "monitoring" / "grafana" / "provisioning" / "alerting" / "rules.yml"


@pytest.fixture(scope="module")
def document() -> dict:
    return yaml.safe_load(RULES.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rules(document) -> list[dict]:
    return [rule for group in document["groups"] for rule in group["rules"]]


def query(rule: dict) -> dict:
    return next(item for item in rule["data"] if item["refId"] == "A")


def test_the_file_is_a_version_1_provisioning_document(document) -> None:
    assert document["apiVersion"] == 1
    assert [group["name"] for group in document["groups"]] == ["jenkins-guardian"]
    assert all(group["folder"] == "JenkinsGuardian" and group["orgId"] == 1 for group in document["groups"])


def test_the_week_7_rules_exist(rules) -> None:
    assert {rule["uid"] for rule in rules} == {
        "approval-waiting-over-1h", "agent-failures-15m", "incident-escalated", "incident-without-report-10m",
    }


def test_rule_uids_and_titles_are_unique(rules) -> None:
    assert len({r["uid"] for r in rules}) == len(rules) == len({r["title"] for r in rules})


def test_each_rule_reduces_the_query_and_thresholds_the_result(rules) -> None:
    for rule in rules:
        steps = {item["refId"]: item for item in rule["data"]}
        assert set(steps) == {"A", "B", "C"}, rule["uid"]
        assert rule["condition"] == "C"
        assert steps["B"]["model"]["type"] == "reduce" and steps["B"]["model"]["expression"] == "A"
        assert steps["C"]["model"]["type"] == "threshold" and steps["C"]["model"]["expression"] == "B"
        assert steps["C"]["model"]["conditions"][0]["evaluator"]["type"] == "gt"


def test_queries_use_the_audit_datasource_and_are_read_only_selects(rules) -> None:
    forbidden = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|grant)\b", re.IGNORECASE)
    for rule in rules:
        item = query(rule)
        assert item["datasourceUid"] == "postgres" and item["model"]["datasource"]["uid"] == "postgres"
        sql = item["model"]["rawSql"]
        assert sql.lstrip().upper().startswith("SELECT"), rule["uid"]
        assert not forbidden.search(sql), rule["uid"]


def test_alert_queries_do_not_use_dashboard_macros_or_variables(rules) -> None:
    for rule in rules:
        assert "$" not in query(rule)["model"]["rawSql"], rule["uid"]


def test_every_rule_says_what_happened_and_how_serious_it_is(rules) -> None:
    for rule in rules:
        assert rule["annotations"]["summary"] and rule["annotations"]["description"], rule["uid"]
        assert rule["labels"]["severity"] in {"warning", "critical"}, rule["uid"]


def test_no_data_does_not_raise_a_false_alarm_but_a_broken_query_does(rules) -> None:
    for rule in rules:
        assert rule["noDataState"] == "OK" and rule["execErrState"] == "Error", rule["uid"]


def test_the_escalation_rule_is_the_critical_one(rules) -> None:
    severities = {rule["uid"]: rule["labels"]["severity"] for rule in rules}
    assert severities["incident-escalated"] == "critical"
