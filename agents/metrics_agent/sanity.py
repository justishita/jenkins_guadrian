"""Sanity checks run before anomaly detection.

A failed check means "cannot conclude", never "anomaly": broken or missing data
must not be turned into a root-cause claim.
"""

import math
from datetime import timedelta
from enum import StrEnum

from pydantic import BaseModel

from .models import QueryResult, Sample
from .queries import QuerySpec
from .window import InvestigationWindow


class SanityIssue(StrEnum):
    EMPTY = "empty"
    TOO_FEW_BASELINE = "too_few_baseline_samples"
    TOO_FEW_INCIDENT = "too_few_incident_samples"
    STALE = "stale"
    OUT_OF_RANGE = "out_of_range"


class SanityResult(BaseModel):
    ok: bool
    issues: list[SanityIssue]
    baseline: list[Sample]
    incident: list[Sample]
    dropped_non_finite: int = 0


def check(
    result: QueryResult,
    spec: QuerySpec,
    window: InvestigationWindow,
    min_baseline: int = 8,
    min_incident: int = 3,
    stale_after: timedelta = timedelta(seconds=60),
) -> SanityResult:
    """Validate a range-query result and split it into baseline and incident samples."""
    if result.empty:
        return SanityResult(ok=False, issues=[SanityIssue.EMPTY], baseline=[], incident=[])

    # One series is expected from the catalog; if several, trust the most populated.
    series = max(result.series, key=lambda s: len(s.samples))
    finite = [s for s in series.samples if math.isfinite(s.value)]
    dropped = len(series.samples) - len(finite)

    baseline = [s for s in finite if window.baseline_start <= s.timestamp < window.start]
    incident = [s for s in finite if window.start <= s.timestamp <= window.end]

    issues: list[SanityIssue] = []
    if len(baseline) < min_baseline:
        issues.append(SanityIssue.TOO_FEW_BASELINE)
    if len(incident) < min_incident:
        issues.append(SanityIssue.TOO_FEW_INCIDENT)
    elif incident[-1].timestamp < window.end - stale_after:
        issues.append(SanityIssue.STALE)

    low, high = spec.sane_range
    if any(not low <= s.value <= high for s in baseline + incident):
        issues.append(SanityIssue.OUT_OF_RANGE)

    return SanityResult(
        ok=not issues, issues=issues, baseline=baseline, incident=incident, dropped_non_finite=dropped
    )
