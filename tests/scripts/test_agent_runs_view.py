"""The agent_runs SQL (monitoring/sql/agent_runs.sql), executed against an in-memory SQLite audit_events table."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SQL = (Path(__file__).resolve().parents[2] / "monitoring" / "sql" / "agent_runs.sql").read_text(encoding="utf-8")
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, actor TEXT, "
        "event_type TEXT, payload TEXT, duration_ms REAL, ok INTEGER, created_at TEXT)"
    )
    yield connection
    connection.close()


def add(db, seconds, actor, event, incident="inc-1", payload=None, duration_ms=None, ok=None):
    db.execute(
        "INSERT INTO audit_events (incident_id, actor, event_type, payload, duration_ms, ok, created_at) VALUES (?,?,?,?,?,?,?)",
        (incident, actor, event, json.dumps(payload or {}), duration_ms, ok, (T0 + timedelta(seconds=seconds)).isoformat()),
    )


def runs(db):
    return [dict(row) for row in db.execute(f"WITH agent_runs AS ({SQL}) SELECT * FROM agent_runs ORDER BY incident_id, agent, started_at, finished_at")]


def test_a_start_and_completion_are_one_run_with_the_result_detail(db):
    add(db, 0, "metrics_agent", "agent_started")
    add(db, 40, "metrics_agent", "agent_completed", payload={"status": "completed", "failure_type": "timeout", "confidence": 0.85}, duration_ms=40000, ok=1)
    [run] = runs(db)
    assert (run["agent"], run["status"], run["failure_type"], run["confidence"], run["duration_ms"], run["source"]) == (
        "metrics_agent", "completed", "timeout", 0.85, 40000, "run",
    )
    assert run["started_at"] and run["finished_at"]


def test_a_failed_run_is_reported_failed_even_without_a_status_in_the_payload(db):
    add(db, 0, "code_agent", "agent_started")
    add(db, 5, "code_agent", "agent_failed", duration_ms=5000, ok=0)
    [run] = runs(db)
    assert run["status"] == "failed" and run["ok"] == 0


def test_redelivery_gives_two_runs(db):
    for offset in (0, 100):
        add(db, offset, "metrics_agent", "agent_started")
        add(db, offset + 10, "metrics_agent", "agent_completed", payload={"status": "completed"}, duration_ms=10000, ok=1)
    assert [r["status"] for r in runs(db)] == ["completed", "completed"]


def test_a_start_with_no_terminal_event_is_running(db):
    add(db, 0, "code_agent", "agent_started")
    [run] = runs(db)
    assert run["status"] == "running" and run["finished_at"] is None


def test_two_starts_without_a_terminal_between_leave_the_first_running(db):
    add(db, 0, "code_agent", "agent_started")
    add(db, 10, "code_agent", "agent_started")
    add(db, 20, "code_agent", "agent_completed", payload={"status": "completed"}, duration_ms=10000, ok=1)
    assert sorted(r["status"] for r in runs(db)) == ["completed", "running"]


def test_actors_and_incidents_are_paired_independently(db):
    add(db, 0, "metrics_agent", "agent_started", incident="a")
    add(db, 1, "code_agent", "agent_started", incident="a")
    add(db, 2, "metrics_agent", "agent_started", incident="b")
    add(db, 3, "code_agent", "agent_completed", incident="a", payload={"status": "completed"}, duration_ms=2000, ok=1)
    add(db, 4, "metrics_agent", "agent_completed", incident="a", payload={"status": "completed"}, duration_ms=4000, ok=1)
    by_key = {(r["incident_id"], r["agent"]): r["status"] for r in runs(db)}
    assert by_key == {("a", "metrics_agent"): "completed", ("a", "code_agent"): "completed", ("b", "metrics_agent"): "running"}


def test_an_agent_with_no_audit_of_its_own_is_covered_by_its_dispatch_event(db):
    add(db, 30, "coordinator", "agent_dispatch", payload={"agent": "jenkins_agent", "attempt": 0, "outcome": "completed", "via": "a2a"}, duration_ms=28000, ok=1)
    [run] = runs(db)
    assert (run["agent"], run["status"], run["duration_ms"], run["source"], run["started_at"]) == ("jenkins_agent", "completed", 28000, "dispatch", None)


def test_a_dispatch_is_not_double_counted_when_the_agent_wrote_its_own_run(db):
    add(db, 0, "metrics_agent", "agent_started")
    add(db, 9, "metrics_agent", "agent_completed", payload={"status": "completed"}, duration_ms=9000, ok=1)
    add(db, 10, "coordinator", "agent_dispatch", payload={"agent": "metrics_agent", "outcome": "completed"}, duration_ms=9500, ok=1)
    assert [r["source"] for r in runs(db)] == ["run"]


def test_timed_out_and_undispatched_agents_appear_with_their_outcome(db):
    add(db, 5, "coordinator", "agent_dispatch", payload={"agent": "code_agent", "outcome": "timed_out"}, duration_ms=120000, ok=0)
    add(db, 5, "coordinator", "agent_dispatch", payload={"agent": "jenkins_agent", "outcome": "not_dispatched"}, duration_ms=0, ok=0)
    assert {r["agent"]: r["status"] for r in runs(db)} == {"code_agent": "timed_out", "jenkins_agent": "not_dispatched"}


def test_other_event_types_are_ignored(db):
    add(db, 0, "metrics_agent", "tool_call", duration_ms=12)
    add(db, 1, "coordinator", "synthesis_completed", payload={"verdict": "timeout"})
    assert runs(db) == []
