"""Cross-metric correlation: a small, explicit rule set applied after per-metric detection.

The pipeline stays `per-metric detection -> per-metric evidence -> correlation -> hypotheses`.
Detectors never look at each other; this module only groups their findings into candidate
causes and notes what supports or contradicts each one. The rules are deliberately few and
written out here, not learned:

1. **Same cause, one hypothesis.** Anomalous metrics that point to the same failure type
   (e.g. CPU and memory -> resource_exhaustion) form one candidate cause; the others
   corroborate the strongest.
2. **Symptoms are absorbed, if they coincide.** Elevated latency that happens at the same time
   as a resource-exhaustion anomaly is treated as a symptom of it, cited as supporting
   evidence, instead of being reported as a second, competing cause. Anomalies that did not
   overlap in time (e.g. a CPU spike minutes before a fresh latency spike) stay separate.
3. **Contradiction is local.** A candidate cause is contradicted only by *relevant catalog
   metrics* (those that map to the same failure type) that were measured and stayed normal.
   Metrics that could not be measured neither support nor contradict.
4. **An outage explains missing data.** When the target is down, other metrics that stopped
   reporting are noted as consistent with the outage, not treated as lacking evidence.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from common.models import FailureTaxonomy

from .findings import Finding

#: Metrics that, when anomalous together with a cause of the given type, are its symptom.
SYMPTOM_OF: dict[str, FailureTaxonomy] = {"latency_p95": FailureTaxonomy.RESOURCE_EXHAUSTION}

#: Two anomalies "coincide" when their elevated periods overlap or lie within this many seconds.
COINCIDENCE_SLACK_SECONDS = 60.0


def coincide(a: Finding, b: Finding) -> bool:
    """Did two metrics' latest anomalies happen at the same time? Unrelated ones must not corroborate."""
    first, second = a.detection, b.detection
    if first is None or second is None:
        return False
    times = (first.run_start_at, first.run_end_at, second.run_start_at, second.run_end_at)
    if any(t is None for t in times):
        return False
    slack = timedelta(seconds=COINCIDENCE_SLACK_SECONDS)
    return first.run_start_at <= second.run_end_at + slack and (  # type: ignore[operator]
        second.run_start_at <= first.run_end_at + slack  # type: ignore[operator]
    )


@dataclass
class CandidateCause:
    failure_type: FailureTaxonomy
    primaries: list[Finding]
    symptoms: list[Finding] = field(default_factory=list)
    contradicting: list[Finding] = field(default_factory=list)
    # Metrics that stopped reporting while the target was down (infra_network_failure only).
    unreported: list[Finding] = field(default_factory=list)


def correlate(findings: list[Finding]) -> list[CandidateCause]:
    anomalous = [f for f in findings if f.anomalous]
    primaries: dict[FailureTaxonomy, list[Finding]] = {}
    for finding in anomalous:
        primaries.setdefault(finding.spec.failure_type, []).append(finding)

    symptoms: dict[FailureTaxonomy, list[Finding]] = {}
    for finding in anomalous:
        cause = SYMPTOM_OF.get(finding.spec.name)
        if (
            cause is not None
            and cause != finding.spec.failure_type
            and any(coincide(finding, primary) for primary in primaries.get(cause, []))
        ):
            primaries[finding.spec.failure_type].remove(finding)
            symptoms.setdefault(cause, []).append(finding)

    causes: list[CandidateCause] = []
    for failure_type, group in primaries.items():
        if not group:
            continue
        causes.append(
            CandidateCause(
                failure_type=failure_type,
                primaries=group,
                symptoms=symptoms.get(failure_type, []),
                contradicting=[f for f in findings if f.normal and f.spec.failure_type == failure_type],
                unreported=_unreported(findings, failure_type),
            )
        )
    return causes


def _unreported(findings: list[Finding], failure_type: FailureTaxonomy) -> list[Finding]:
    if failure_type != FailureTaxonomy.INFRA_NETWORK_FAILURE:
        return []
    return [f for f in findings if f.issues and f.spec.failure_type != failure_type]
