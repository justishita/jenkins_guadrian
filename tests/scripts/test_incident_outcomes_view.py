"""The incident_outcomes SQL (monitoring/sql/incident_outcomes.sql), run against an in-memory SQLite copy of the tables."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SQL = (Path(__file__).resolve().parents[2] / "monitoring" / "sql" / "incident_outcomes.sql").read_text(encoding="utf-8")
T0 = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)


def stamp(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE incidents (id TEXT PRIMARY KEY, job_name TEXT, status TEXT, remediation_attempt INTEGER, created_at TEXT);
        CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, actor TEXT, event_type TEXT,
                                   payload TEXT, ok INTEGER, created_at TEXT);
        CREATE TABLE remediation_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, attempt INTEGER, status TEXT);
        CREATE TABLE approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, attempt INTEGER, decision TEXT);
        """
    )
    yield connection
    connection.close()


def incident(db, name, status="OPEN", attempt=0):
    db.execute("INSERT INTO incidents VALUES (?,?,?,?,?)", (name, "target-app/main", status, attempt, stamp(0)))


def event(db, name, kind, seconds, payload=None, ok=None):
    db.execute(
        "INSERT INTO audit_events (incident_id, actor, event_type, payload, ok, created_at) VALUES (?,?,?,?,?,?)",
        (name, "x", kind, json.dumps(payload or {}), ok, stamp(seconds)),
    )


def proposal(db, name, status, attempt=0):
    db.execute("INSERT INTO remediation_proposals (incident_id, attempt, status) VALUES (?,?,?)", (name, attempt, status))


def outcomes(db):
    return {row["incident_id"]: dict(row) for row in db.execute(f"WITH incident_outcomes AS ({SQL}) SELECT * FROM incident_outcomes")}


def test_a_new_incident_with_nothing_else_is_open(db):
    incident(db, "a")
    assert outcomes(db)["a"]["outcome"] == "open" and outcomes(db)["a"]["closed_at"] is None


def test_a_passing_revalidation_is_resolved_by_fix_and_closed_at_the_status_event(db):
    incident(db, "a", "RESOLVED", attempt=1)
    event(db, "a", "synthesis_completed", 30, {"verdict": "timeout"})
    event(db, "a", "approval_decided", 600, {"decision": "approved", "reviewer": "rev@x.io"}, ok=1)
    event(db, "a", "revalidation_completed", 900, ok=1)
    event(db, "a", "incident_status_changed", 901, {"from": "OPEN", "to": "RESOLVED"})
    row = outcomes(db)["a"]
    assert (row["outcome"], row["verdict"], row["approver"], row["decision"], row["revalidations"]) == ("resolved_by_fix", "timeout", "rev@x.io", "approved", 1)
    assert row["closed_at"] == stamp(901)


def test_an_older_resolved_incident_without_a_status_event_closes_at_the_passing_revalidation(db):
    incident(db, "a", "RESOLVED")
    event(db, "a", "revalidation_completed", 500, ok=0)
    event(db, "a", "revalidation_completed", 700, ok=1)
    assert outcomes(db)["a"]["closed_at"] == stamp(700)


def test_a_closed_rejected_incident_is_rejected_with_who_and_when(db):
    incident(db, "a", "CLOSED_REJECTED")
    event(db, "a", "approval_decided", 100, {"decision": "rejected", "reviewer": "rev@x.io"}, ok=0)
    event(db, "a", "incident_status_changed", 100, {"from": "OPEN", "to": "CLOSED_REJECTED"})
    row = outcomes(db)["a"]
    assert (row["outcome"], row["approver"], row["closed_at"]) == ("rejected", "rev@x.io", stamp(100))


def test_a_rejection_recorded_before_the_closing_status_existed_still_counts_as_rejected(db):
    incident(db, "a", "OPEN")
    event(db, "a", "approval_decided", 100, {"decision": "rejected", "reviewer": "rev@x.io"}, ok=0)
    row = outcomes(db)["a"]
    assert row["outcome"] == "rejected" and row["closed_at"] is None


def test_an_escalated_incident_is_escalated(db):
    incident(db, "a", "ESCALATED", attempt=3)
    for n in range(3):
        event(db, "a", "revalidation_completed", 100 * (n + 1), ok=0)
    event(db, "a", "incident_status_changed", 301, {"from": "OPEN", "to": "ESCALATED"})
    row = outcomes(db)["a"]
    assert (row["outcome"], row["revalidations"], row["attempt"], row["closed_at"]) == ("escalated", 3, 3, stamp(301))


def test_an_approved_fix_waiting_for_jenkins_is_awaiting_validation(db):
    incident(db, "a")
    proposal(db, "a", "approved")
    event(db, "a", "approval_decided", 100, {"decision": "approved", "reviewer": "rev@x.io"}, ok=1)
    assert outcomes(db)["a"]["outcome"] == "awaiting_validation"


def test_a_proposal_with_no_decision_is_awaiting_approval_until_an_approval_row_exists(db):
    incident(db, "a")
    proposal(db, "a", "awaiting_approval")
    assert outcomes(db)["a"]["outcome"] == "awaiting_approval"
    db.execute("INSERT INTO approvals (incident_id, attempt, decision) VALUES ('a', 0, 'approved')")
    assert outcomes(db)["a"]["outcome"] == "open"  # decided (the audit row would say which); no longer waiting


def test_a_policy_refused_proposal_is_policy_blocked(db):
    incident(db, "a")
    proposal(db, "a", "policy_rejected")
    assert outcomes(db)["a"]["outcome"] == "policy_blocked"


def test_each_incident_gets_exactly_one_row_and_the_latest_report_wins(db):
    incident(db, "a")
    incident(db, "b")
    event(db, "a", "synthesis_completed", 10, {"verdict": "timeout"})
    event(db, "a", "synthesis_completed", 50, {"verdict": "dependency_regression"})
    rows = outcomes(db)
    assert set(rows) == {"a", "b"} and rows["a"]["verdict"] == "dependency_regression" and rows["b"]["verdict"] is None
