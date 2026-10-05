"""The per-metric outcome that feeds correlation and hypothesis building."""

from dataclasses import dataclass, field

from .anomaly import Detection
from .queries import QuerySpec
from .sanity import SanityIssue


@dataclass
class Finding:
    """Outcome of one catalog query. Exactly one of detection / issues / error explains it."""

    spec: QuerySpec
    detection: Detection | None = None
    issues: list[SanityIssue] = field(default_factory=list)
    error: str | None = None
    # Data quality (0..1) from the sanity step; meaningful when `detection` is set.
    quality: float = 0.0

    @property
    def evidence_id(self) -> str:
        return f"metric-{self.spec.name}"

    @property
    def anomalous(self) -> bool:
        return self.detection is not None and self.detection.anomalous

    @property
    def normal(self) -> bool:
        """Sane data that showed nothing."""
        return self.detection is not None and not self.detection.anomalous
