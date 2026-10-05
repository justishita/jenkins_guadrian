"""Metrics agent -> shared Evidence Store, next to another agent's evidence.

Starts from a message shaped exactly like the one backend/routes/webhooks.py publishes
as `incident.created`, runs the real consumer-side code path (parse -> MetricsAgent ->
Investigator -> SharedStoreEvidenceWriter) against a fake Prometheus, and checks what the
Coordinator will see when it calls `read_all(incident_id)`.
"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from agents.metrics_agent.consumer import MetricsAgent, parse_event
from agents.metrics_agent.investigator import InvestigationConfig, Investigator
from agents.metrics_agent.planner import CatalogPlanner
from agents.metrics_agent.prometheus_tool import PrometheusUnavailableError
from agents.metrics_agent.queries import AVAILABILITY, LATENCY_P95
from agents.metrics_agent.store import SharedStoreEvidenceWriter
from common.evidence_store import FileEvidenceStore
from common.models import Evidence, EvidenceHypothesis, EvidenceItem, FailureTaxonomy
from tests.metrics_agent.helpers import (
    FAILURE_TIME,
    NOW,
    FakeTool,
    make_window,
    step_up,
)


def webhook_message(incident_id: str) -> bytes:
    """Same keys and formats as `event_message` in backend/routes/webhooks.py."""
    return json.dumps(
        {
            "incident_id": incident_id,
            "job_name": "target-app/main",
            "build_number": 42,
            "build_url": "http://jenkins:8080/job/target-app/job/main/42/",
            "branch": "main",
            "git_commit": "a1b2c3d4e5f67890abcdef1234567890abcdef12",
            "failed_stage": "Test",
            "remediation_attempt": 0,
            "timestamp": FAILURE_TIME.isoformat(),
            "received_at": FAILURE_TIME.isoformat(),
        }
    ).encode()


def jenkins_agent_evidence(incident_id: str) -> Evidence:
    """What the Jenkins agent contributes for the same incident (a different diagnosis)."""
    return Evidence(
        incident_id=incident_id,
        agent="jenkins_agent",
        created_at=NOW,
        status="completed",
        failure_type=FailureTaxonomy.CODE_TEST_FAILURE,
        summary="pytest failed in the Test stage.",
        root_cause_hypotheses=[
            EvidenceHypothesis(
                hypothesis="A unit test assertion failed.",
                failure_type=FailureTaxonomy.CODE_TEST_FAILURE,
                confidence=0.9,
                supporting_evidence=["console-1"],
            )
        ],
        evidence_items=[
            EvidenceItem(id="console-1", kind="log_excerpt", source="jenkins", content="FAILED test_orders.py"),
        ],
        confidence=0.9,
        failed_stage="Test",
        redaction_applied=True,
    )


def metrics_agent(store: FileEvidenceStore, tool: FakeTool) -> MetricsAgent:
    investigator = Investigator(
        tool,  # type: ignore[arg-type]
        CatalogPlanner(),
        SharedStoreEvidenceWriter(store),
        InvestigationConfig(),
        clock=lambda: NOW,
        sleep=lambda _seconds: None,
    )
    return MetricsAgent(investigator)


def test_webhook_message_ends_up_as_metrics_evidence_beside_jenkins_evidence(tmp_path: Path) -> None:
    store = FileEvidenceStore(tmp_path)
    incident_id = str(uuid4())
    tool = FakeTool({LATENCY_P95.promql: step_up(make_window(), 0.007, 2.0)})

    metrics_agent(store, tool).handle_incident(parse_event(webhook_message(incident_id)))
    asyncio.run(store.write(jenkins_agent_evidence(incident_id)))

    evidence = {e.agent: e for e in asyncio.run(store.read_all(incident_id))}
    assert sorted(evidence) == ["jenkins_agent", "metrics_agent"]
    metrics = evidence["metrics_agent"]
    assert str(metrics.incident_id) == incident_id
    assert metrics.failure_type == FailureTaxonomy.TIMEOUT
    assert metrics.failed_stage == "Test"
    assert {i.kind for i in metrics.evidence_items} == {"metric"}
    # The two agents disagree; resolving that is the Coordinator's job, not the agents'.
    assert evidence["jenkins_agent"].failure_type == FailureTaxonomy.CODE_TEST_FAILURE
    assert all(e.redaction_applied for e in evidence.values())


def test_prometheus_outage_still_leaves_readable_evidence_for_the_coordinator(tmp_path: Path) -> None:
    store = FileEvidenceStore(tmp_path)
    incident_id = str(uuid4())
    tool = FakeTool(errors={AVAILABILITY.promql: PrometheusUnavailableError("down")})

    metrics_agent(store, tool).handle_incident(parse_event(webhook_message(incident_id)))
    asyncio.run(store.write(jenkins_agent_evidence(incident_id)))

    metrics = asyncio.run(store.read(incident_id, "metrics_agent"))
    assert metrics is not None
    assert metrics.status == "failed" and metrics.failure_type == FailureTaxonomy.UNKNOWN
    assert len(asyncio.run(store.read_all(incident_id))) == 2


def test_redelivered_incident_replaces_metrics_evidence_only(tmp_path: Path) -> None:
    store = FileEvidenceStore(tmp_path)
    incident_id = str(uuid4())
    asyncio.run(store.write(jenkins_agent_evidence(incident_id)))
    agent = metrics_agent(store, FakeTool())

    agent.handle_incident(parse_event(webhook_message(incident_id)))
    agent.handle_incident(parse_event(webhook_message(incident_id)))

    assert len(list((tmp_path / incident_id).glob("*.json"))) == 2
    stored = json.loads((tmp_path / incident_id / "metrics_agent.json").read_text(encoding="utf-8"))
    assert stored["version"] == 2
    jenkins = json.loads((tmp_path / incident_id / "jenkins_agent.json").read_text(encoding="utf-8"))
    assert jenkins["version"] == 1
