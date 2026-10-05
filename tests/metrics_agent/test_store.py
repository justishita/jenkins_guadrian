import json
from pathlib import Path
from uuid import uuid4

import pytest

from agents.metrics_agent.evidence import (
    EvidenceItem,
    EvidenceRecord,
    RootCauseHypothesis,
)
from agents.metrics_agent.store import EvidenceValidationError, LocalJsonEvidenceWriter
from common.models import FailureTaxonomy


def valid_record(**overrides: object) -> EvidenceRecord:
    data: dict[str, object] = {
        "incident_id": uuid4(),
        "status": "completed",
        "failure_type": FailureTaxonomy.TIMEOUT,
        "summary": "latency spike",
        "root_cause_hypotheses": [
            RootCauseHypothesis(hypothesis="p95 spiked", failure_type=FailureTaxonomy.TIMEOUT, confidence=0.8)
        ],
        "evidence_items": [EvidenceItem(id="metric-latency_p95", content="{}")],
        "confidence": 0.8,
    }
    data.update(overrides)
    return EvidenceRecord.model_validate(data)


def test_writes_schema_valid_json_with_utc_z_timestamp(tmp_path: Path) -> None:
    record = valid_record()
    LocalJsonEvidenceWriter(tmp_path).write(record)
    stored = json.loads((tmp_path / f"{record.incident_id}.metrics_agent.json").read_text(encoding="utf-8"))
    assert stored["agent"] == "metrics_agent"
    assert stored["schema_version"] == "0.1-stub"
    assert stored["created_at"].endswith("Z")
    assert stored["failure_type"] == "timeout"


def test_rewriting_same_incident_is_idempotent(tmp_path: Path) -> None:
    writer = LocalJsonEvidenceWriter(tmp_path)
    incident_id = uuid4()
    writer.write(valid_record(incident_id=incident_id, summary="first"))
    writer.write(valid_record(incident_id=incident_id, summary="second"))
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["summary"] == "second"
    assert not list(tmp_path.glob("*.tmp"))


def test_schema_violation_is_rejected_and_nothing_is_written(tmp_path: Path) -> None:
    schema = tmp_path / "strict.json"
    schema.write_text(json.dumps({"type": "object", "required": ["no_such_field"]}), encoding="utf-8")
    out = tmp_path / "out"
    with pytest.raises(EvidenceValidationError, match="no_such_field"):
        LocalJsonEvidenceWriter(out, schema).write(valid_record())
    assert not out.exists() or not list(out.glob("*"))


def test_confidence_outside_unit_interval_rejected() -> None:
    with pytest.raises(ValueError):
        valid_record(confidence=1.5)


def test_failure_taxonomy_matches_decisions_doc_exactly() -> None:
    assert {t.value for t in FailureTaxonomy} == {
        "code_test_failure",
        "build_compilation_failure",
        "dependency_regression",
        "timeout",
        "resource_exhaustion",
        "infra_network_failure",
        "config_error",
        "auth_failure",
        "flaky_test",
        "deployment_failure",
        "unknown",
    }


def test_free_text_secrets_are_redacted_before_they_reach_evidence() -> None:
    from agents.metrics_agent.evidence import item_content

    content = item_content({"error": "login failed password=hunter2 Authorization: Bearer abc.def.ghi"})
    assert "hunter2" not in content and "abc.def.ghi" not in content
    assert "[REDACTED]" in content


def test_failed_stage_from_the_event_is_redacted_and_record_declares_redaction() -> None:
    record = valid_record(failed_stage="Deploy api_key=sk-12345")
    assert "sk-12345" not in record.failed_stage  # type: ignore[operator]
    assert record.redaction_applied is True
