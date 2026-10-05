"""Metrics investigation pipeline.

window -> plan -> retrieve (PrometheusTool) -> sanity -> anomaly -> hypotheses -> evidence.

The investigator never talks HTTP itself and never raises for investigation
problems: they become `failed` evidence so the Coordinator still sees the outcome.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from common.models import Evidence, EvidenceItem, FailureTaxonomy, ToolCallRecord

from .anomaly import detect
from .confidence import inputs_from
from .evidence import make_evidence, metric_item
from .findings import Finding
from .hypotheses import synthesize
from .models import IncidentCreatedEvent
from .planner import QueryPlanner
from .prometheus_tool import PrometheusError, PrometheusTool, PrometheusUnavailableError
from .sanity import check
from .store import EvidenceWriter
from .window import InvestigationWindow, build_window

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InvestigationConfig:
    lookback: timedelta = timedelta(minutes=5)
    tail: timedelta = timedelta(seconds=60)
    baseline: timedelta = timedelta(minutes=10)
    step: str = "15s"
    # Prometheus only sees a failure after the next scrape and step-aligned evaluation,
    # so querying right at the failure time misses it. Wait this long past the failure.
    settle: timedelta = timedelta(seconds=30)
    min_baseline_samples: int = 8
    min_incident_samples: int = 3


_STEP_UNITS = {"s": 1, "m": 60, "h": 3600}


def _step_seconds(step: str) -> int:
    """Seconds in a Prometheus duration like '15s' (config validates the format)."""
    return int(step[:-1]) * _STEP_UNITS[step[-1]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Investigator:
    def __init__(
        self,
        tool: PrometheusTool,
        planner: QueryPlanner,
        writer: EvidenceWriter,
        config: InvestigationConfig | None = None,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._tool = tool
        self._planner = planner
        self._writer = writer
        self._config = config or InvestigationConfig()
        self._clock = clock
        self._sleep = sleep

    def investigate(self, event: IncidentCreatedEvent) -> Evidence:
        """Investigate one incident and persist exactly one evidence record."""
        try:
            record = self._investigate(event)
        except Exception:  # boundary: any bug must still yield evidence, not a dead consumer
            logger.exception("investigation crashed", extra={"incident_id": str(event.incident_id)})
            record = self._failed(event, "Internal error during metrics investigation.", [])
        self._writer.write(record)
        logger.info(
            "investigation complete",
            extra={
                "incident_id": str(event.incident_id),
                "status": record.status,
                "failure_type": record.failure_type,
                "confidence": record.confidence,
            },
        )
        return record

    def _investigate(self, event: IncidentCreatedEvent) -> Evidence:
        cfg = self._config
        now = self._clock()
        failure_time = event.timestamp or event.received_at or now
        now = self._settle(failure_time, now)
        try:
            window = build_window(failure_time, cfg.lookback, cfg.tail, cfg.baseline, now=now)
        except ValueError as exc:
            return self._failed(event, f"Invalid investigation window: {exc}", [])

        step = timedelta(seconds=_step_seconds(cfg.step))
        findings: list[Finding] = []
        tool_calls: list[ToolCallRecord] = []
        for spec in self._planner.plan(event):
            started = time.perf_counter()
            args = {
                "promql": spec.promql,
                "start": window.baseline_start.isoformat(),
                "end": window.end.isoformat(),
                "step": cfg.step,
                # When the query was issued; the audit trail uses it as the record's own time.
                "issued_at": self._clock().isoformat(),
            }
            try:
                result = self._tool.query_range(spec.promql, window.baseline_start, window.end, cfg.step)
            except PrometheusUnavailableError as exc:
                tool_calls.append(self._call(args, started, ok=False))
                return self._failed(event, f"Prometheus unavailable: {exc}", tool_calls)
            except PrometheusError as exc:
                tool_calls.append(self._call(args, started, ok=False))
                findings.append(Finding(spec, error=str(exc)))
                continue
            tool_calls.append(self._call(args, started, ok=True))

            sanity = check(
                result,
                spec,
                window,
                min_baseline=cfg.min_baseline_samples,
                min_incident=cfg.min_incident_samples,
                step=step,
            )
            if sanity.ok:
                detection = detect(spec, sanity.baseline, sanity.incident, failure_time)
                findings.append(Finding(spec, detection=detection, quality=sanity.quality))
            else:
                findings.append(Finding(spec, issues=sanity.issues, quality=sanity.quality))

        verdict = synthesize(findings)
        return make_evidence(
            incident_id=event.incident_id,
            status=verdict.status,
            failure_type=verdict.failure_type,
            summary=verdict.summary,
            confidence=verdict.confidence,
            hypotheses=verdict.hypotheses,
            items=[self._item(f, window) for f in findings],
            tool_calls=tool_calls,
            next_steps=verdict.next_steps,
            failed_stage=event.failed_stage,
        )

    def _settle(self, failure_time: datetime, now: datetime) -> datetime:
        """Let Prometheus catch up with a very recent failure; the wait is capped at `settle`."""
        settle = self._config.settle
        wait = min((failure_time + settle - now).total_seconds(), settle.total_seconds())
        if wait <= 0:
            return now
        logger.info("waiting for metrics to settle", extra={"seconds": round(wait, 1)})
        self._sleep(wait)
        return self._clock()

    @staticmethod
    def _call(args: dict[str, str], started: float, ok: bool) -> ToolCallRecord:
        return ToolCallRecord(
            tool="prometheus.query_range",
            args=args,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            ok=ok,
        )

    @staticmethod
    def _item(finding: Finding, window: InvestigationWindow) -> EvidenceItem:
        if finding.error:
            outcome = "error"
        elif finding.issues:
            outcome = "unusable"
        elif finding.detection and finding.detection.anomalous:
            outcome = "anomalous"
        else:
            outcome = "normal"
        return metric_item(
            finding.evidence_id,
            window.end,
            {
                "metric": finding.spec.name,
                "promql": finding.spec.promql,
                "unit": finding.spec.unit,
                "outcome": outcome,
                "window": {
                    "baseline_start": window.baseline_start,
                    "start": window.start,
                    "end": window.end,
                },
                "detection": finding.detection.model_dump(mode="json") if finding.detection else None,
                "confidence_inputs": (
                    inputs_from(finding.detection, finding.quality).as_dict() if finding.anomalous else None
                ),
                "issues": [str(i) for i in finding.issues],
                "error": finding.error,
            },
        )

    @staticmethod
    def _failed(event: IncidentCreatedEvent, reason: str, tool_calls: list[ToolCallRecord]) -> Evidence:
        return make_evidence(
            incident_id=event.incident_id,
            status="failed",
            failure_type=FailureTaxonomy.UNKNOWN,
            summary=reason,
            confidence=0.0,
            tool_calls=tool_calls,
            failed_stage=event.failed_stage,
        )
