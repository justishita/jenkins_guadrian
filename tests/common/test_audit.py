"""Tests for the shared audit trail.

**Owner:** P3. The trail is the project's traceability guarantee, so the things worth
testing are that it cannot leak a secret, that it records failures as well as
successes, and that losing it never takes an investigation down with it.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from common.audit import (
	MAX_PAYLOAD_VALUE_CHARS,
	AuditLog,
	AuditRecord,
	BestEffortAuditLog,
	FileAuditLog,
	build_audit_log,
)


INCIDENT = UUID("11111111-2222-3333-4444-555555555555")


@pytest.fixture
def audit_log(tmp_path: Path) -> FileAuditLog:
	return FileAuditLog(tmp_path / "audit")


def record(**overrides: object) -> AuditRecord:
	payload: dict[str, object] = {
		"incident_id": INCIDENT,
		"actor": "code_agent",
		"event_type": "tool_call",
	}
	payload.update(overrides)
	return AuditRecord(**payload)  # type: ignore[arg-type]


def test_summary_is_redacted() -> None:
	entry = record(summary="calling GitHub with token=ghp_abcdef1234567890")
	assert "ghp_abcdef1234567890" not in entry.summary
	assert "[REDACTED]" in entry.summary


def test_payload_is_redacted_at_every_depth() -> None:
	entry = record(
		payload={
			"headers": {"authorization": "Bearer ghp_abcdef1234567890"},
			"attempts": [{"api_key": "sk-secret-value"}],
		}
	)
	serialized = json.dumps(entry.payload)
	assert "ghp_abcdef1234567890" not in serialized
	assert "sk-secret-value" not in serialized
	assert serialized.count("[REDACTED]") == 2


def test_oversized_payload_values_are_truncated() -> None:
	entry = record(payload={"console": "x" * (MAX_PAYLOAD_VALUE_CHARS * 2)})
	console = entry.payload["console"]
	assert isinstance(console, str)
	assert console.endswith("...[truncated]")
	assert len(console) < MAX_PAYLOAD_VALUE_CHARS * 2


def test_non_string_payload_values_survive_redaction() -> None:
	entry = record(payload={"duration": 12.5, "ok": True, "count": 3, "missing": None})
	assert entry.payload == {"duration": 12.5, "ok": True, "count": 3, "missing": None}


def test_naive_timestamps_are_treated_as_utc() -> None:
	from datetime import datetime

	entry = record(created_at=datetime(2026, 10, 5, 9, 30))
	assert entry.created_at.utcoffset() is not None
	assert entry.created_at.isoformat().endswith("+00:00")


def test_negative_duration_is_rejected() -> None:
	with pytest.raises(ValueError):
		record(duration_ms=-1)


@pytest.mark.asyncio
async def test_records_round_trip_in_write_order(audit_log: FileAuditLog) -> None:
	for event_type in ("agent_started", "tool_call", "agent_completed"):
		await audit_log.record(record(event_type=event_type))

	stored = await audit_log.read(INCIDENT)
	assert [entry.event_type for entry in stored] == [
		"agent_started",
		"tool_call",
		"agent_completed",
	]


@pytest.mark.asyncio
async def test_reading_an_unknown_incident_is_empty_not_an_error(
	audit_log: FileAuditLog,
) -> None:
	assert await audit_log.read(uuid4()) == []


@pytest.mark.asyncio
async def test_incidents_are_kept_in_separate_files(audit_log: FileAuditLog, tmp_path: Path) -> None:
	other = uuid4()
	await audit_log.record(record())
	await audit_log.record(record(incident_id=other))

	assert len(await audit_log.read(INCIDENT)) == 1
	assert len(await audit_log.read(other)) == 1
	assert len(list((tmp_path / "audit").glob("*.jsonl"))) == 2


@pytest.mark.asyncio
async def test_agent_run_brackets_a_successful_run(audit_log: FileAuditLog) -> None:
	async with audit_log.agent_run(INCIDENT, "code_agent") as details:
		details["failure_type"] = "config_error"

	started, completed = await audit_log.read(INCIDENT)
	assert started.event_type == "agent_started"
	assert completed.event_type == "agent_completed"
	assert completed.ok is True
	assert completed.payload == {"failure_type": "config_error"}
	assert completed.duration_ms is not None and completed.duration_ms >= 0


@pytest.mark.asyncio
async def test_agent_run_records_a_failure_and_re_raises(audit_log: FileAuditLog) -> None:
	with pytest.raises(RuntimeError, match="github unreachable"):
		async with audit_log.agent_run(INCIDENT, "code_agent") as details:
			details["tool_calls"] = 2
			raise RuntimeError("github unreachable")

	_, failed = await audit_log.read(INCIDENT)
	assert failed.event_type == "agent_failed"
	assert failed.ok is False
	assert "github unreachable" in failed.summary
	assert failed.payload == {"tool_calls": 2}


@pytest.mark.asyncio
async def test_agent_run_failure_summary_is_redacted(audit_log: FileAuditLog) -> None:
	with pytest.raises(RuntimeError):
		async with audit_log.agent_run(INCIDENT, "code_agent"):
			raise RuntimeError("rejected request with token=ghp_abcdef1234567890")

	_, failed = await audit_log.read(INCIDENT)
	assert "ghp_abcdef1234567890" not in failed.summary


class _BrokenAuditLog(AuditLog):
	async def record(self, record: AuditRecord) -> None:
		raise OSError("disk is full")

	async def read(self, incident_id: UUID | str) -> list[AuditRecord]:
		raise OSError("disk is full")


@pytest.mark.asyncio
async def test_best_effort_swallows_write_failures(caplog: pytest.LogCaptureFixture) -> None:
	log = BestEffortAuditLog(_BrokenAuditLog())
	await log.record(record())  # must not raise: the investigation outranks its trail
	assert "dropping audit record" in caplog.text


@pytest.mark.asyncio
async def test_best_effort_still_surfaces_read_failures() -> None:
	log = BestEffortAuditLog(_BrokenAuditLog())
	with pytest.raises(OSError):
		await log.read(INCIDENT)


def test_builder_defaults_to_the_file_trail_without_a_database(tmp_path: Path) -> None:
	log = build_audit_log(root=tmp_path, best_effort=False)
	assert isinstance(log, FileAuditLog)


def test_builder_wraps_in_best_effort_by_default(tmp_path: Path) -> None:
	assert isinstance(build_audit_log(root=tmp_path), BestEffortAuditLog)
