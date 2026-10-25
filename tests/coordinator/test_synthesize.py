"""Synthesis over a real incident (1bf496ad) and the collect -> fuse -> store -> audit chain."""

import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from common.audit import FileAuditLog
from common.evidence_store import FileEvidenceStore
from common.models import FailureTaxonomy
from coordinator.fused import synthesize_fused
from coordinator.report import (
    Completeness,
    ConflictStatus,
    FusedReport,
    render_markdown,
)
from coordinator.report_store import ReportStore

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "incident_1bf496ad"
INCIDENT = UUID("1bf496ad-2958-4adc-9a0f-dfb38ae3aa2c")
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def blackboard(tmp_path):
    target = tmp_path / "evidence" / str(INCIDENT)
    target.mkdir(parents=True)
    for source in FIXTURE.glob("*.json"):
        shutil.copy(source, target / source.name)
    return FileEvidenceStore(tmp_path / "evidence")


class Fused:
    """The fused synthesis bound to one blackboard, with a fixed clock."""

    def __init__(self, store, **kwargs):
        self._store, self._kwargs = store, kwargs

    async def synthesize(self, incident_id):
        return await synthesize_fused(self._store, incident_id, now=NOW, **self._kwargs)


@pytest.fixture
def coordinator(blackboard, tmp_path):
    return Fused(blackboard, report_store=ReportStore(tmp_path / "reports"), audit=FileAuditLog(tmp_path / "audit"))


async def test_real_incident_fuses_to_the_jenkins_verdict_with_config_error_as_alternative(coordinator):
    report = await coordinator.synthesize(INCIDENT)

    assert report.verdict == FailureTaxonomy.DEPENDENCY_REGRESSION
    # Jenkins 0.90 x 1.0, discounted by the capped 0.30 contradiction from the Code agent (below).
    assert report.ranking[0].undiscounted_score == pytest.approx(0.9)
    assert report.confidence == pytest.approx(0.63)
    assert [s.failure_type for s in report.ranking] == [
        FailureTaxonomy.DEPENDENCY_REGRESSION,
        FailureTaxonomy.CONFIG_ERROR,
    ]
    config_error = report.ranking[1]
    assert config_error.score == pytest.approx(0.44)  # 0.55 raw x 0.8 code-agent relevance
    assert config_error.contributions[0].confidence == 0.55

    assert report.completeness == Completeness.COMPLETE
    assert report.conflict_status == ConflictStatus.NONE
    assert report.ready_for_policy_review


async def test_metrics_found_nothing_and_is_neutral_without_declared_coverage(coordinator):
    report = await coordinator.synthesize(INCIDENT)
    metrics = next(v for v in report.agent_verdicts if v.agent == "metrics_agent")
    assert not metrics.voted and metrics.vote_note == "no failure type identified"
    assert metrics.investigation_coverage is None
    assert not [n for s in report.ranking for n in s.negative_contributions if n.agent == "metrics_agent"]


async def test_code_agents_contradicting_files_discount_dependency_regression_up_to_the_cap(coordinator):
    # The Code agent's own primary is config_error; its dependency_regression hypothesis
    # cites 11 changed source files as contradicting. Separate evidence ids are separate
    # penalties, bounded by the cap, so the 0.90 Jenkins vote loses at most 0.30.
    report = await coordinator.synthesize(INCIDENT)
    dependency = report.ranking[0]
    assert dependency.discount == pytest.approx(0.30)
    negatives = dependency.negative_contributions
    assert {n.agent for n in negatives} == {"code_agent"}
    assert {n.kind.value for n in negatives} == {"explicit_contradiction"}
    assert len(negatives) == 11 and any(n.status.value == "capped" for n in negatives)
    assert sum(n.applied_penalty for n in negatives) == pytest.approx(0.30)


async def test_jenkins_secondary_hypotheses_are_shown_but_do_not_outvote_its_primary(coordinator):
    report = await coordinator.synthesize(INCIDENT)
    jenkins = next(v for v in report.agent_verdicts if v.agent == "jenkins_agent")
    assert {"auth_failure", "timeout", "config_error"} <= set(jenkins.alternative_hypotheses)
    assert FailureTaxonomy.AUTH_FAILURE not in {s.failure_type for s in report.ranking}


async def test_synthesis_does_not_alter_the_agents_evidence(coordinator, blackboard):
    before = await blackboard.read_all(INCIDENT)
    await coordinator.synthesize(INCIDENT)
    assert await blackboard.read_all(INCIDENT) == before


async def test_report_is_stored_as_json_and_markdown_and_round_trips(coordinator, tmp_path):
    report = await coordinator.synthesize(INCIDENT)
    store = ReportStore(tmp_path / "reports")
    assert (tmp_path / "reports" / f"{INCIDENT}.md").read_text(encoding="utf-8") == render_markdown(report)
    loaded = await store.read(INCIDENT)
    assert isinstance(loaded, FusedReport) and loaded == report


async def test_markdown_shows_raw_confidence_weight_and_both_method_scores(coordinator):
    markdown = render_markdown(await coordinator.synthesize(INCIDENT))
    assert "`dependency_regression`" in markdown
    assert "0.55 x 0.80 = 0.44" in markdown
    assert "uncalibrated" in markdown
    assert "Other method" in markdown


async def test_synthesis_is_audited(coordinator, tmp_path):
    await coordinator.synthesize(INCIDENT)
    records = await FileAuditLog(tmp_path / "audit").read(INCIDENT)
    [record] = [r for r in records if r.event_type == "synthesis_completed"]
    assert record.actor == "coordinator"
    assert record.payload["verdict"] == "dependency_regression"
    assert record.payload["completeness"] == "complete"
    assert record.ok is True


async def test_unknown_incident_yields_an_incomplete_unknown_report(coordinator):
    report = await coordinator.synthesize("22222222-2222-4222-8222-222222222222")
    assert report.verdict == FailureTaxonomy.UNKNOWN
    assert report.completeness == Completeness.INCOMPLETE
    assert not report.ready_for_policy_review


async def test_synthesize_works_without_storage_or_audit(blackboard):
    report = await Fused(blackboard).synthesize(INCIDENT)
    assert report.verdict == FailureTaxonomy.DEPENDENCY_REGRESSION
