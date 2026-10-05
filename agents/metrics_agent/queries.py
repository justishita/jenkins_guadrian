"""Catalog of known-good PromQL (verified in monitoring/README.md).

Each spec says what to ask Prometheus, how to judge the answer, and which failure
type a confirmed anomaly points to.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from .taxonomy import FailureTaxonomy


class DetectorKind(StrEnum):
    SPIKE = "spike"
    SUSTAINED_INCREASE = "sustained_increase"
    AVAILABILITY = "availability"


class QuerySpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    promql: str
    unit: str
    detector: DetectorKind
    failure_type: FailureTaxonomy
    # Smallest absolute change that is operationally meaningful.
    min_effect: float
    # Values outside this closed range mean the data is broken, not anomalous.
    sane_range: tuple[float, float]
    z_threshold: float = 6.0


AVAILABILITY = QuerySpec(
    name="target_availability",
    promql='up{job="target_app"}',
    unit="bool",
    detector=DetectorKind.AVAILABILITY,
    failure_type=FailureTaxonomy.INFRA_NETWORK_FAILURE,
    min_effect=1.0,
    sane_range=(0.0, 1.0),
)

LATENCY_P95 = QuerySpec(
    name="latency_p95",
    promql=(
        "histogram_quantile(0.95, sum by (le) "
        '(rate(http_request_duration_seconds_bucket{job="target_app"}[1m])))'
    ),
    unit="s",
    detector=DetectorKind.SPIKE,
    failure_type=FailureTaxonomy.TIMEOUT,
    min_effect=0.25,
    sane_range=(0.0, 3600.0),
)

CPU_RATE = QuerySpec(
    name="cpu_rate",
    promql='rate(process_cpu_seconds{job="target_app"}[1m])',
    unit="cores",
    detector=DetectorKind.SPIKE,
    failure_type=FailureTaxonomy.RESOURCE_EXHAUSTION,
    min_effect=0.03,
    sane_range=(0.0, 1024.0),
)

MEMORY_RSS = QuerySpec(
    name="memory_rss",
    promql='process_memory_bytes{job="target_app"}',
    unit="bytes",
    detector=DetectorKind.SUSTAINED_INCREASE,
    failure_type=FailureTaxonomy.RESOURCE_EXHAUSTION,
    min_effect=5 * 1024 * 1024,
    sane_range=(0.0, 1e13),
)

CATALOG: tuple[QuerySpec, ...] = (AVAILABILITY, LATENCY_P95, CPU_RATE, MEMORY_RSS)
