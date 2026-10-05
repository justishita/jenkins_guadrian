import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agents.metrics_agent.evidence import item_content, make_evidence, metric_item
from agents.metrics_agent.store import SharedStoreEvidenceWriter
from common.evidence_store import FileEvidenceStore
from common.models import Evidence, EvidenceHypothesis, FailureTaxonomy

EVIDENCE_ID = "metric-latency_p95"
EVIDENCE_TIME = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)


def valid_record(**overrides: Any) -> Evidence:
    kwargs: dict[str, Any] = {
        "incident_id": uuid4(),
        "status": "completed",
        "failure_type": FailureTaxonomy.TIMEOUT,
        "summary": "latency spike",
        "confidence": 0.8,
        "hypotheses": [
            EvidenceHypothesis(
                hypothesis="p95 spiked",
                failure_type=FailureTaxonomy.TIMEOUT,
                confidence=0.8,
                supporting_evidence=[EVIDENCE_ID],
            )
        ],
        "items": [metric_item(EVIDENCE_ID, EVIDENCE_TIME,{"outcome": "anomalous"})],
    }
    kwargs.update(overrides)
    return make_evidence(**kwargs)


def test_builds_the_shared_evidence_model_for_the_metrics_agent() -> None:
    record = valid_record()
    assert isinstance(record, Evidence)
    assert record.agent == "metrics_agent"
    assert record.evidence_items[0].kind == "metric"


def test_writes_into_the_shared_store_layout(tmp_path: Path) -> None:
    record = valid_record()
    SharedStoreEvidenceWriter(FileEvidenceStore(tmp_path)).write(record)
    stored_path = tmp_path / str(record.incident_id) / "metrics_agent.json"
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    assert stored["agent"] == "metrics_agent"
    assert stored["created_at"].endswith("Z")
    assert stored["failure_type"] == "timeout"
    assert asyncio.run(FileEvidenceStore(tmp_path).read(record.incident_id, "metrics_agent")) == record


def test_agent_evidence_sits_next_to_other_agents_for_the_coordinator(tmp_path: Path) -> None:
    store = FileEvidenceStore(tmp_path)
    incident_id = uuid4()
    SharedStoreEvidenceWriter(store).write(valid_record(incident_id=incident_id))
    other = valid_record(incident_id=incident_id).model_copy(update={"agent": "jenkins_agent"})
    asyncio.run(store.write(other))
    agents = sorted(e.agent for e in asyncio.run(store.read_all(incident_id)))
    assert agents == ["jenkins_agent", "metrics_agent"]


def test_redelivery_replaces_the_document_and_bumps_the_version(tmp_path: Path) -> None:
    writer = SharedStoreEvidenceWriter(FileEvidenceStore(tmp_path))
    incident_id = uuid4()
    writer.write(valid_record(incident_id=incident_id, summary="first"))
    writer.write(valid_record(incident_id=incident_id, summary="second"))
    files = list((tmp_path / str(incident_id)).glob("*.json"))
    assert len(files) == 1
    stored = json.loads(files[0].read_text(encoding="utf-8"))
    assert stored["summary"] == "second" and stored["version"] == 2


def test_write_works_from_a_worker_thread_like_the_consumer_uses() -> None:
    async def scenario(root: Path) -> None:
        record = valid_record()
        await asyncio.to_thread(SharedStoreEvidenceWriter(FileEvidenceStore(root)).write, record)
        assert (root / str(record.incident_id) / "metrics_agent.json").is_file()

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


def test_store_refuses_evidence_that_was_not_redacted(tmp_path: Path) -> None:
    unredacted = valid_record().model_copy(update={"redaction_applied": False})
    with pytest.raises(ValueError, match="redaction"):
        SharedStoreEvidenceWriter(FileEvidenceStore(tmp_path)).write(unredacted)
    assert not list(tmp_path.glob("*"))


def test_records_conform_to_the_checked_in_stub_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).resolve().parents[2] / "common" / "evidence_schema_stub.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    payload = valid_record().model_dump(mode="json", exclude_none=True)
    jsonschema.Draft7Validator(schema).validate(payload)


def test_confidence_outside_unit_interval_rejected() -> None:
    with pytest.raises(ValueError):
        valid_record(confidence=1.5)


def test_hypothesis_citing_missing_evidence_is_rejected_by_the_shared_contract() -> None:
    bad = EvidenceHypothesis(
        hypothesis="x", failure_type=FailureTaxonomy.TIMEOUT, confidence=0.5, supporting_evidence=["metric-ghost"]
    )
    with pytest.raises(ValueError, match="unknown evidence IDs"):
        valid_record(hypotheses=[bad])


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
    content = item_content({"error": "login failed password=hunter2 Authorization: Bearer abc.def.ghi"})
    assert "hunter2" not in content and "abc.def.ghi" not in content
    assert "[REDACTED]" in content


def test_event_derived_text_is_redacted_and_record_declares_redaction() -> None:
    record = valid_record(failed_stage="Deploy api_key=sk-12345", summary="boom token=abc123")
    assert "sk-12345" not in (record.failed_stage or "")
    assert "abc123" not in record.summary
    assert record.redaction_applied is True
