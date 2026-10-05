"""Cross-metric correlation and how it shows up in the hypotheses."""

from datetime import timedelta

from agents.metrics_agent.anomaly import Detection
from agents.metrics_agent.correlation import coincide, correlate
from agents.metrics_agent.findings import Finding
from agents.metrics_agent.hypotheses import synthesize
from agents.metrics_agent.queries import (
    AVAILABILITY,
    CPU_RATE,
    LATENCY_P95,
    MEMORY_RSS,
    QuerySpec,
)
from agents.metrics_agent.sanity import SanityIssue
from common.models import FailureTaxonomy
from tests.metrics_agent.helpers import FAILURE_TIME


def detection(
    *,
    anomalous: bool,
    strength: float = 0.8,
    run: int = 6,
    gap: float | None = 0.0,
    span: tuple[float, float] = (0.0, 0.0),
) -> Detection:
    """`span` is (first, last) elevated time in seconds relative to the failure."""
    return Detection(
        detector=LATENCY_P95.detector,
        anomalous=anomalous,
        direction="up" if anomalous else "none",
        baseline_value=1.0,
        observed_value=2.0,
        score=5.0,
        strength=strength if anomalous else 0.0,
        elevated_samples=run if anomalous else 0,
        longest_run=run if anomalous else 0,
        incident_samples=25,
        run_start_at=FAILURE_TIME + timedelta(seconds=span[0]) if anomalous else None,
        run_end_at=FAILURE_TIME + timedelta(seconds=span[1]) if anomalous else None,
        gap_to_failure_seconds=gap if anomalous else None,
        still_elevated=anomalous,
        window_start=FAILURE_TIME,
        window_end=FAILURE_TIME,
        detail="peak 2 vs baseline median 1",
    )


def anomalous(spec: QuerySpec, **kwargs: object) -> Finding:
    return Finding(spec, detection=detection(anomalous=True, **kwargs), quality=1.0)  # type: ignore[arg-type]


def normal(spec: QuerySpec) -> Finding:
    return Finding(spec, detection=detection(anomalous=False), quality=1.0)


def unusable(spec: QuerySpec) -> Finding:
    return Finding(spec, issues=[SanityIssue.EMPTY])


# --- correlate() ------------------------------------------------------------------------


def test_cpu_and_latency_together_form_one_cause_with_latency_as_a_symptom() -> None:
    causes = correlate([normal(AVAILABILITY), anomalous(LATENCY_P95), anomalous(CPU_RATE), normal(MEMORY_RSS)])
    assert len(causes) == 1
    cause = causes[0]
    assert cause.failure_type == FailureTaxonomy.RESOURCE_EXHAUSTION
    assert [f.spec.name for f in cause.primaries] == ["cpu_rate"]
    assert [f.spec.name for f in cause.symptoms] == ["latency_p95"]
    assert [f.spec.name for f in cause.contradicting] == ["memory_rss"]


def test_latency_alone_is_its_own_timeout_cause() -> None:
    (cause,) = correlate([normal(AVAILABILITY), anomalous(LATENCY_P95), normal(CPU_RATE), normal(MEMORY_RSS)])
    assert cause.failure_type == FailureTaxonomy.TIMEOUT
    assert cause.symptoms == []


def test_cpu_and_memory_corroborate_each_other_as_one_cause() -> None:
    (cause,) = correlate([anomalous(CPU_RATE), anomalous(MEMORY_RSS), normal(LATENCY_P95)])
    assert {f.spec.name for f in cause.primaries} == {"cpu_rate", "memory_rss"}
    assert cause.contradicting == []


def test_contradiction_comes_only_from_relevant_catalog_metrics() -> None:
    # A latency (timeout) cause is not contradicted by CPU or memory being normal.
    (cause,) = correlate([anomalous(LATENCY_P95), normal(CPU_RATE), normal(MEMORY_RSS), normal(AVAILABILITY)])
    assert cause.contradicting == []


def test_a_metric_that_could_not_be_measured_neither_supports_nor_contradicts() -> None:
    (cause,) = correlate([anomalous(CPU_RATE), unusable(MEMORY_RSS)])
    assert cause.contradicting == []


def test_an_outage_explains_the_metrics_that_stopped_reporting() -> None:
    (cause,) = correlate(
        [anomalous(AVAILABILITY), unusable(LATENCY_P95), unusable(CPU_RATE), unusable(MEMORY_RSS)]
    )
    assert cause.failure_type == FailureTaxonomy.INFRA_NETWORK_FAILURE
    assert len(cause.unreported) == 3 and cause.contradicting == []


def test_independent_anomalies_stay_separate_causes() -> None:
    causes = correlate([anomalous(AVAILABILITY), anomalous(MEMORY_RSS)])
    assert {c.failure_type for c in causes} == {
        FailureTaxonomy.INFRA_NETWORK_FAILURE,
        FailureTaxonomy.RESOURCE_EXHAUSTION,
    }


def test_nothing_anomalous_means_no_causes() -> None:
    assert correlate([normal(AVAILABILITY), normal(CPU_RATE), unusable(LATENCY_P95)]) == []


# --- the reasoning reaches the hypothesis -----------------------------------------------


def test_hypothesis_states_the_cross_metric_reasoning_and_cites_its_evidence() -> None:
    verdict = synthesize([normal(AVAILABILITY), anomalous(LATENCY_P95), anomalous(CPU_RATE), normal(MEMORY_RSS)])
    (hypothesis,) = verdict.hypotheses
    assert "coincides with elevated latency_p95" in hypothesis.hypothesis
    assert "memory_rss stayed normal" in hypothesis.hypothesis
    assert hypothesis.supporting_evidence == ["metric-cpu_rate", "metric-latency_p95"]
    assert hypothesis.contradicting_evidence == ["metric-memory_rss"]
    assert verdict.status == "completed" and verdict.confidence == hypothesis.confidence


def test_corroborated_cause_outranks_the_same_evidence_uncorroborated() -> None:
    corroborated = synthesize([anomalous(CPU_RATE), anomalous(LATENCY_P95), normal(MEMORY_RSS)])
    alone = synthesize([anomalous(CPU_RATE), normal(LATENCY_P95), normal(MEMORY_RSS)])
    assert corroborated.confidence > alone.confidence


def test_normal_confidence_reflects_data_quality() -> None:
    good = synthesize([normal(CPU_RATE)])
    poor = synthesize([Finding(CPU_RATE, detection=detection(anomalous=False), quality=0.4)])
    assert good.status == poor.status == "insufficient_evidence"
    assert 0 < poor.confidence < good.confidence


# --- anomalies must coincide in time to corroborate -------------------------------------


def test_a_fresh_latency_spike_is_not_the_symptom_of_a_cpu_spike_minutes_earlier() -> None:
    """Regression from a live run: an old, recovered CPU spike swallowed the current latency anomaly."""
    cpu = anomalous(CPU_RATE, span=(-285, -240), gap=240)
    latency = anomalous(LATENCY_P95, span=(-120, 0))
    causes = correlate([cpu, latency, normal(MEMORY_RSS), normal(AVAILABILITY)])
    assert {c.failure_type for c in causes} == {FailureTaxonomy.TIMEOUT, FailureTaxonomy.RESOURCE_EXHAUSTION}
    assert all(c.symptoms == [] for c in causes)


def test_the_fresh_anomaly_outranks_the_old_one_and_the_old_one_is_marked_recovered_not_corroborating() -> None:
    cpu = anomalous(CPU_RATE, span=(-285, -240), gap=240)
    latency = anomalous(LATENCY_P95, span=(-120, 0))
    verdict = synthesize([cpu, latency, normal(MEMORY_RSS), normal(AVAILABILITY)])
    assert [h.failure_type for h in verdict.hypotheses] == [
        FailureTaxonomy.TIMEOUT,
        FailureTaxonomy.RESOURCE_EXHAUSTION,
    ]
    assert verdict.failure_type == FailureTaxonomy.TIMEOUT
    assert "coincides" not in verdict.hypotheses[0].hypothesis


def test_anomalies_that_overlap_or_nearly_touch_coincide() -> None:
    cpu = anomalous(CPU_RATE, span=(-120, -60))
    assert coincide(cpu, anomalous(LATENCY_P95, span=(-90, 0)))  # overlap
    assert coincide(cpu, anomalous(LATENCY_P95, span=(-30, 0)))  # within the 60s slack
    assert coincide(anomalous(LATENCY_P95, span=(-30, 0)), cpu)  # symmetric


def test_anomalies_far_apart_or_without_timing_do_not_coincide() -> None:
    cpu = anomalous(CPU_RATE, span=(-285, -240))
    assert not coincide(cpu, anomalous(LATENCY_P95, span=(-120, 0)))
    assert not coincide(cpu, normal(LATENCY_P95))


def test_a_same_type_anomaly_from_earlier_is_cited_but_does_not_corroborate() -> None:
    old_cpu = anomalous(CPU_RATE, span=(-285, -240), gap=240)
    memory = anomalous(MEMORY_RSS, span=(-60, 0))
    (hypothesis,) = synthesize([old_cpu, memory, normal(LATENCY_P95), normal(AVAILABILITY)]).hypotheses
    assert "separate earlier anomaly: cpu_rate" in hypothesis.hypothesis
    assert "also anomalous" not in hypothesis.hypothesis
    assert set(hypothesis.supporting_evidence) == {"metric-memory_rss", "metric-cpu_rate"}
