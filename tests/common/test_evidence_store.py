import json
from datetime import datetime, timezone

import pytest

from common.evidence_store import EvidenceStore, FileEvidenceStore
from common.models import Evidence, FailureTaxonomy


def make_evidence(*, redaction_applied: bool = True) -> Evidence:
    return Evidence(
        incident_id="4c7a2f7d-bfb6-42cf-a79a-d4540e0d7644",
        created_at=datetime.now(timezone.utc),
        status="insufficient_evidence",
        failure_type=FailureTaxonomy.UNKNOWN,
        summary="Insufficient evidence.",
        confidence=0.2,
        redaction_applied=redaction_applied,
    )


@pytest.mark.asyncio
async def test_file_store_implements_interface_and_upserts_with_version(tmp_path):
    store = FileEvidenceStore(tmp_path / "evidence")
    evidence = make_evidence()

    assert isinstance(store, EvidenceStore)
    await store.write(evidence)
    await store.write(evidence)

    filepath = (
        tmp_path
        / "evidence"
        / str(evidence.incident_id)
        / f"{evidence.agent}.json"
    )
    document = json.loads(filepath.read_text(encoding="utf-8"))
    assert document["version"] == 2
    assert len(list(filepath.parent.glob("*.json"))) == 1
    assert await store.read(evidence.incident_id, evidence.agent) == evidence
    assert await store.read_all(evidence.incident_id) == [evidence]
    assert await store.read("d8fb2fb4-43f4-48de-94dc-a4d17b647b5d", evidence.agent) is None
    assert await store.read_all("d8fb2fb4-43f4-48de-94dc-a4d17b647b5d") == []


@pytest.mark.asyncio
async def test_file_store_rejects_unredacted_evidence(tmp_path):
    store = FileEvidenceStore(tmp_path / "evidence")

    with pytest.raises(ValueError, match="without redaction"):
        await store.write(make_evidence(redaction_applied=False))

    assert not (tmp_path / "evidence").exists()


@pytest.mark.asyncio
async def test_atomic_write_failure_preserves_existing_file_and_cleans_temp(
    tmp_path,
    monkeypatch,
):
    store = FileEvidenceStore(tmp_path / "evidence")
    evidence = make_evidence()
    await store.write(evidence)
    filepath = (
        tmp_path
        / "evidence"
        / str(evidence.incident_id)
        / f"{evidence.agent}.json"
    )
    original = filepath.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr("common.evidence_store.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated replacement failure"):
        await store.write(evidence)

    assert filepath.read_bytes() == original
    assert list(filepath.parent.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_file_store_rejects_path_traversal_agent(tmp_path):
    store = FileEvidenceStore(tmp_path / "evidence")

    with pytest.raises(ValueError, match="file-name component"):
        await store.read("4c7a2f7d-bfb6-42cf-a79a-d4540e0d7644", "../outside")


@pytest.mark.asyncio
async def test_file_store_reads_evidence_from_multiple_agents(tmp_path):
    store = FileEvidenceStore(tmp_path / "evidence")
    jenkins_evidence = make_evidence()
    metrics_evidence = Evidence.model_validate(
        {
            **jenkins_evidence.model_dump(mode="json"),
            "agent": "metrics_agent",
        }
    )

    await store.write(jenkins_evidence)
    await store.write(metrics_evidence)

    assert await store.read_all(jenkins_evidence.incident_id) == [
        jenkins_evidence,
        metrics_evidence,
    ]
    assert await store.read(jenkins_evidence.incident_id, "metrics_agent") == metrics_evidence
