"""RabbitMQ-to-A2A dispatcher for the investigation agents, and the single synthesis trigger."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any

import aio_pika
import httpx
from fastapi import FastAPI, HTTPException, Request, status
from pydantic import ValidationError

from common.a2a_contract import AGENT_NAMES
from common.audit import AuditLog, AuditRecord, build_audit_log
from common.blackboard import EvidenceStore as BlackboardEvidenceStore
from common.blackboard import get_blackboard
from common.evidence_store import FileEvidenceStore
from common.models import IncidentCreatedEvent
from coordinator.agent import CoordinatorAgent
from coordinator.revisions import PersistResult, Revision, RevisionStore, evidence_fingerprint
from coordinator.stores import MergedEvidenceStore
from coordinator.synthesis import IncidentReport

logger = logging.getLogger(__name__)


class AgentOutcome(str, Enum):
    """What the Coordinator saw for one agent's part of an investigation.

    ``completed`` means the dispatch (or the bounded blackboard wait) succeeded; whether the
    evidence is valid is checked separately when the report is fused.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    NOT_DISPATCHED = "not_dispatched"


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


class CoordinatorRuntime:
    """Own the incident queue and dispatch Jenkins work over A2A."""

    def __init__(self) -> None:
        self.rabbitmq_url = os.getenv("RABBITMQ_URL", "")
        self.a2a_token = os.getenv("A2A_SHARED_TOKEN", "")
        self.jenkins_agent_url = os.getenv(
            "JENKINS_AGENT_URL",
            "http://jenkins-agent:9101",
        ).rstrip("/")
        self.timeout_seconds = float(os.getenv("A2A_TIMEOUT_SECONDS", "70"))
        self.agent_urls = {
            "jenkins_agent": self.jenkins_agent_url,
            "metrics_agent": os.getenv("METRICS_AGENT_URL", "http://metrics-agent:9102").rstrip("/"),
            "code_agent": os.getenv("CODE_AGENT_URL", "http://code-agent:9103").rstrip("/"),
        }
        # Agents whose evidence the report must contain before it counts as complete.
        self.required_agents = _csv_env("COORDINATOR_REQUIRED_AGENTS", AGENT_NAMES)
        # Agents the Coordinator triggers over A2A. Any other required agent is triggered by
        # its own RabbitMQ consumer, so the Coordinator waits (boundedly) for its evidence.
        # TRIGGER_MODE=a2a triggers all three agents over A2A; with ``rabbitmq`` only the Jenkins
        # agent (A2A-only) is. The Metrics and Code agents read the same variable, so exactly one
        # path investigates any incident.
        self.trigger_mode = os.getenv("TRIGGER_MODE", "a2a").strip().lower()
        default_a2a = AGENT_NAMES if self.trigger_mode == "a2a" else ("jenkins_agent",)
        self.a2a_agents = _csv_env("COORDINATOR_A2A_AGENTS", default_a2a)
        self.watch_timeout_seconds = float(os.getenv("COORDINATOR_WATCH_TIMEOUT_SECONDS", "120"))
        self.watch_poll_seconds = float(os.getenv("COORDINATOR_WATCH_POLL_SECONDS", "2"))
        # Per-incident outcomes, and one in-flight/finished dispatch per (incident, attempt)
        # so a redelivered message never investigates or synthesizes twice.
        self.outcomes: dict[str, dict[str, str]] = {}
        self._dispatches: dict[tuple[str, int], asyncio.Future[dict[str, str]]] = {}
        # Persisted report revisions + the audit trail (set in ``start``; ``None`` disables both,
        # which keeps unit tests free of disk writes). See coordinator/revisions.py.
        self.audit: AuditLog | None = None
        self.revisions: RevisionStore | None = None
        self.evidence_root = Path(os.getenv("EVIDENCE_DIR", "data/evidence"))
        self.blackboard_root = Path(os.getenv("BLACKBOARD_DIR", "data/blackboard"))
        # How long after the initial report a late agent may still produce a revision.
        self.late_window_seconds = float(os.getenv("COORDINATOR_LATE_WINDOW_SECONDS", "300"))
        self.late_poll_seconds = float(os.getenv("COORDINATOR_LATE_POLL_SECONDS", "5"))
        # Restart recovery only looks at incidents whose evidence changed recently.
        self.recovery_window_seconds = float(os.getenv("COORDINATOR_RECOVERY_WINDOW_SECONDS", "3600"))
        self._late_tasks: set[asyncio.Task[None]] = set()
        self.exchange_name = os.getenv("EXCHANGE_NAME", "incidents")
        self.queue_name = os.getenv("COORDINATOR_QUEUE_NAME", "coordinator.incident.created")
        self.connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.queue: aio_pika.abc.AbstractQueue | None = None
        self.consumer_tag: str | None = None
        self.client: httpx.AsyncClient | None = None
        self.adk_agent: CoordinatorAgent | None = None
        # Latest fused report per incident, so the running service's verdict can be
        # inspected without re-reading the blackboard.
        self.reports: dict[str, IncidentReport] = {}

    async def start(self) -> None:
        if not self.rabbitmq_url:
            raise RuntimeError("RABBITMQ_URL must be configured")
        if not self.a2a_token:
            raise RuntimeError("A2A_SHARED_TOKEN must be configured")

        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            headers={"Authorization": f"Bearer {self.a2a_token}"},
        )
        # Read evidence from the blackboard and the evidence directory: the Jenkins agent writes
        # only to the former, the Metrics and Code agents to the latter (and mirror to the former
        # when triggered over A2A). Reading just one would report a healthy agent as missing.
        self.adk_agent = CoordinatorAgent(
            MergedEvidenceStore(
                BlackboardEvidenceStore(get_blackboard()),
                FileEvidenceStore(Path(os.getenv("EVIDENCE_DIR", "data/evidence"))),
            )
        )
        self.audit = build_audit_log(
            database_url=os.getenv("DATABASE_URL") or None,
            root=Path(os.getenv("AUDIT_DIR", "data/audit")),
        )
        self.revisions = RevisionStore(Path(os.getenv("REPORTS_DIR", "data/reports")))
        try:
            await self.recover()
        except Exception as error:  # noqa: BLE001 - recovery must never stop the service starting
            logger.error("Coordinator recovery failed (%s)", type(error).__name__)
        self.connection = await self._connect_with_retry()
        channel = await self.connection.channel()
        await channel.set_qos(prefetch_count=1)
        exchange = await channel.declare_exchange(
            self.exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        self.queue = await channel.declare_queue(self.queue_name, durable=True)
        await self.queue.bind(exchange, routing_key="incident.created")
        await self.queue.bind(exchange, routing_key="incident.reinvestigate")
        self.consumer_tag = await self.queue.consume(self._handle_message)
        logger.info("Coordinator consuming incident.created events")

    async def _connect_with_retry(self) -> aio_pika.abc.AbstractRobustConnection:
        retries = 4
        for attempt in range(retries + 1):
            try:
                return await aio_pika.connect_robust(self.rabbitmq_url, timeout=10)
            except (OSError, asyncio.TimeoutError, aio_pika.exceptions.AMQPException) as error:
                if attempt == retries:
                    raise RuntimeError("Coordinator could not connect to RabbitMQ") from error
                delay_seconds = min(0.5 * (2**attempt), 4.0)
                logger.warning(
                    "Coordinator RabbitMQ connect failed (%s); retrying",
                    type(error).__name__,
                )
                await asyncio.sleep(delay_seconds)
        raise RuntimeError("Coordinator RabbitMQ connection attempts were exhausted")

    async def _handle_message(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        try:
            async with message.process(requeue=True):
                body = json.loads(message.body)
                routing_key = message.routing_key or ""

                if routing_key == "incident.reinvestigate" or body.get("previous_failure_summary"):
                    await self._dispatch_reinvestigation(body)
                    return

                try:
                    event = IncidentCreatedEvent.model_validate(body)
                except (ValueError, ValidationError) as error:
                    logger.error(
                        "Discarding invalid incident.created message (%s)",
                        type(error).__name__,
                    )
                    return
                await self.dispatch_all(event)
        except Exception as error:
            logger.error(
                "Coordinator dispatch failed for incident message (%s)",
                type(error).__name__,
            )
            raise

    async def dispatch_all(self, event: IncidentCreatedEvent) -> dict[str, str]:
        """Run every required agent, then synthesize once; returns ``{agent: outcome}``.

        Idempotent per ``(incident_id, attempt)``: a redelivered message awaits the same
        run instead of starting another. A failed or timed-out agent never raises here; it
        is recorded as an outcome and makes the report incomplete.
        """
        key = (str(event.incident_id), event.remediation_attempt)
        running = self._dispatches.get(key)
        if running is None:
            running = asyncio.ensure_future(self._dispatch_all(event))
            self._dispatches[key] = running
        return await asyncio.shield(running)

    async def _dispatch_all(self, event: IncidentCreatedEvent) -> dict[str, str]:
        agents = [agent for agent in AGENT_NAMES if agent in self.required_agents]
        results = await asyncio.gather(*(self._run_agent(agent, event) for agent in agents))
        outcomes = {agent: result.value for agent, result in zip(agents, results, strict=True)}
        self.outcomes[str(event.incident_id)] = outcomes
        logger.info(
            "Coordinator collected agent outcomes",
            extra={"incident_id": str(event.incident_id), "outcomes": outcomes},
        )
        await self._synthesize(event, outcomes)
        if self.revisions is not None and any(o != AgentOutcome.COMPLETED.value for o in outcomes.values()):
            task = asyncio.ensure_future(self._late_watch(event, dict(outcomes)))
            self._late_tasks.add(task)
            task.add_done_callback(self._late_tasks.discard)
        return outcomes

    async def _late_watch(self, event: IncidentCreatedEvent, outcomes: dict[str, str]) -> None:
        """Give agents that missed the deadline a bounded chance to land a report revision.

        A late result is applied only if it is real evidence (not a failed document) for an
        agent whose outcome was not ``completed``; ``RevisionStore`` then refuses it if that
        exact evidence set is already applied or the attempt is stale.
        """
        if self.adk_agent is None:
            return
        deadline = time.monotonic() + self.late_window_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(self.late_poll_seconds)
            view = await self.adk_agent.collect(event.incident_id)
            usable = {item.agent for item in view.evidence if item.status != "failed"}
            arrived = [a for a, o in outcomes.items() if o != AgentOutcome.COMPLETED.value and a in usable]
            if not arrived:
                continue
            for agent in arrived:
                outcomes[agent] = AgentOutcome.COMPLETED.value
            self.outcomes[str(event.incident_id)] = dict(outcomes)
            await self._synthesize(event, outcomes, reason="late_evidence")
            if all(o == AgentOutcome.COMPLETED.value for o in outcomes.values()):
                return

    async def _record_revision(
        self, event: IncidentCreatedEvent, report: IncidentReport, fingerprint: str, reason: str
    ) -> None:
        """Persist the report as a revision (idempotent), then audit it (repairable)."""
        if self.revisions is None:
            return
        result, revision = await self.revisions.persist(
            report, attempt=event.remediation_attempt, fingerprint=fingerprint, reason=reason
        )
        if result is PersistResult.CREATED:
            await self._audit_revision(revision)
        elif result is PersistResult.STALE:
            logger.warning(
                "Ignoring a report for an older attempt",
                extra={"incident_id": str(event.incident_id), "attempt": event.remediation_attempt},
            )

    async def _audit_revision(self, revision: Revision) -> None:
        if self.audit is None:
            return
        report = revision.report
        await self.audit.record(
            AuditRecord(
                incident_id=revision.incident_id,
                actor="coordinator",
                event_type=revision.event_type,
                summary=report.summary,
                payload={
                    "report_revision_id": revision.report_revision_id,
                    "revision": revision.revision,
                    "attempt": revision.attempt,
                    "reason": revision.reason,
                    "evidence_fingerprint": revision.fingerprint,
                    "verdict": report.failure_type.value,
                    "confidence": report.confidence,
                    "completeness": "complete" if report.complete else "incomplete",
                    "conflict_status": "conflicted" if report.status == "conflicting" else "none",
                    "fusion_method": "agreement",
                    "missing_agents": list(report.missing_agents),
                    "failed_agents": list(report.failed_agents),
                    "agent_outcomes": dict(report.agent_outcomes),
                    "ready_for_policy_review": report.actionable,
                },
                ok=True,
            )
        )

    async def recover(self) -> int:
        """Repair after a restart without ever duplicating a revision or an audit record.

        1. A revision whose audit event is missing (crash between the two writes) gets the
           event only.
        2. A recently-changed incident whose current evidence is not yet in any revision is
           synthesized; agents with no evidence are recorded ``timed_out``. If the evidence
           is already applied, nothing happens, so restarting twice is harmless.
        Returns the number of audit events repaired plus revisions created.
        """
        if self.revisions is None or self.adk_agent is None:
            return 0
        changed = await self._repair_missing_audit()
        for incident_id in await self._recent_incidents():
            view = await self.adk_agent.collect(incident_id)
            if not view.evidence:
                continue
            existing = await self.revisions.revisions(incident_id)
            attempt = max((item.attempt for item in existing), default=0)
            fingerprint = evidence_fingerprint(view.evidence)
            if any(item.attempt == attempt and item.fingerprint == fingerprint for item in existing):
                continue
            outcomes = {
                agent: (AgentOutcome.COMPLETED.value if agent in view.reporting_agents else AgentOutcome.TIMED_OUT.value)
                for agent in AGENT_NAMES
                if agent in self.required_agents
            }
            report = await self.adk_agent.synthesize(incident_id, outcomes=outcomes)
            if report is None:
                continue
            result, revision = await self.revisions.persist(
                report, attempt=attempt, fingerprint=fingerprint, reason="recovered"
            )
            if result is PersistResult.CREATED:
                await self._audit_revision(revision)
                self.reports[str(incident_id)] = report
                changed += 1
        return changed

    async def _repair_missing_audit(self) -> int:
        if self.revisions is None or self.audit is None:
            return 0
        repaired = 0
        for incident_id in await self.revisions.incidents():
            known = {
                record.payload.get("report_revision_id")
                for record in await self.audit.read(incident_id)
                if record.event_type in ("synthesis_completed", "synthesis_revised")
            }
            for revision in await self.revisions.revisions(incident_id):
                if revision.report_revision_id not in known:
                    await self._audit_revision(revision)
                    repaired += 1
        return repaired

    async def _recent_incidents(self) -> list[str]:
        def scan() -> list[str]:
            cutoff = time.time() - self.recovery_window_seconds
            found: set[str] = set()
            for root, pattern in ((self.evidence_root, "*.json"), (self.blackboard_root, "*.jsonl")):
                if not root.is_dir():
                    continue
                for directory in root.iterdir():
                    files = list(directory.glob(pattern)) if directory.is_dir() else []
                    if files and max(path.stat().st_mtime for path in files) >= cutoff:
                        found.add(directory.name)
            return sorted(found)

        return await asyncio.to_thread(scan)

    async def _run_agent(self, agent: str, event: IncidentCreatedEvent) -> AgentOutcome:
        if agent in self.a2a_agents:
            return await self._dispatch_agent_task(agent, event)
        return await self._watch_blackboard(agent, event)

    async def _dispatch_agent_task(self, agent: str, event: IncidentCreatedEvent) -> AgentOutcome:
        """POST one A2A task with the same retry policy as the Jenkins dispatch."""
        if self.client is None:
            raise RuntimeError("Coordinator HTTP client has not started")
        url = f"{self.agent_urls[agent]}/tasks"
        payload = {"incident_id": str(event.incident_id), "attempt": event.remediation_attempt}
        outcome = AgentOutcome.FAILED
        for attempt in range(3):
            try:
                response = await self.client.post(url, json=payload)
                if response.status_code == 504:
                    return AgentOutcome.TIMED_OUT
                if response.status_code >= 500:
                    outcome = AgentOutcome.FAILED
                elif response.status_code >= 400:
                    return AgentOutcome.FAILED
                else:
                    result = response.json()
                    valid = (
                        isinstance(result, dict)
                        and result.get("status") == "completed"
                        and result.get("incident_id") == str(event.incident_id)
                        and result.get("attempt") == event.remediation_attempt
                        and isinstance(result.get("blackboard_ref"), dict)
                    )
                    return AgentOutcome.COMPLETED if valid else AgentOutcome.FAILED
            except httpx.TimeoutException:
                outcome = AgentOutcome.TIMED_OUT
            except (httpx.TransportError, ValueError):
                outcome = AgentOutcome.FAILED
            if attempt < 2:
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        logger.warning(
            "A2A dispatch gave up",
            extra={"incident_id": str(event.incident_id), "agent": agent, "outcome": outcome.value},
        )
        return outcome

    async def _watch_blackboard(self, agent: str, event: IncidentCreatedEvent) -> AgentOutcome:
        """Wait, with a deadline, for an agent that triggers itself to publish evidence.

        The bound is what stops this fallback from recreating the premature or never-ending
        synthesis problem: after ``watch_timeout_seconds`` the agent is ``timed_out``.
        """
        if self.adk_agent is None:
            return AgentOutcome.NOT_DISPATCHED
        deadline = time.monotonic() + self.watch_timeout_seconds
        while True:
            view = await self.adk_agent.collect(event.incident_id)
            if agent in view.reporting_agents:
                return AgentOutcome.COMPLETED
            if time.monotonic() >= deadline:
                return AgentOutcome.TIMED_OUT
            await asyncio.sleep(self.watch_poll_seconds)

    async def dispatch_jenkins_task(self, event: IncidentCreatedEvent) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError("Coordinator HTTP client has not started")

        url = f"{self.jenkins_agent_url}/tasks"
        payload = {
            "incident_id": str(event.incident_id),
            "attempt": event.remediation_attempt,
        }
        for attempt in range(3):
            try:
                response = await self.client.post(url, json=payload)
                if response.status_code >= 500:
                    if attempt < 2:
                        await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
                        continue
                response.raise_for_status()
                result = response.json()
                if (
                    not isinstance(result, dict)
                    or result.get("status") != "completed"
                    or result.get("incident_id") != str(event.incident_id)
                    or result.get("attempt") != event.remediation_attempt
                    or not isinstance(result.get("blackboard_ref"), dict)
                ):
                    raise RuntimeError("Jenkins agent returned an invalid A2A response")

                if self.adk_agent is not None:
                    view = await self.adk_agent.collect(event.incident_id)
                    logger.info(
                        "Coordinator read agent evidence from Blackboard",
                        extra={
                            "incident_id": str(event.incident_id),
                            "reporting_agents": list(view.reporting_agents),
                        },
                    )
                    await self._synthesize(event)
                return result
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt == 2:
                    raise
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
            except httpx.HTTPStatusError as error:
                if error.response.status_code < 500 or attempt == 2:
                    raise
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        raise RuntimeError("Jenkins A2A dispatch attempts were exhausted")

    async def _dispatch_reinvestigation(self, body: dict[str, Any]) -> dict[str, Any]:
        """Forward an incident.reinvestigate event to the Jenkins agent via A2A."""
        if self.client is None:
            raise RuntimeError("Coordinator HTTP client has not started")

        incident_id = body.get("incident_id", "")
        attempt = int(body.get("attempt", 1))
        previous_failure_summary = body.get("previous_failure_summary", "")
        new_build_url = body.get("new_build_url", "")

        url = f"{self.jenkins_agent_url}/tasks"
        payload = {
            "incident_id": str(incident_id),
            "attempt": attempt,
            "previous_failure_summary": previous_failure_summary,
            "new_build_url": new_build_url,
        }
        for retry in range(3):
            try:
                response = await self.client.post(url, json=payload)
                if response.status_code >= 500:
                    if retry < 2:
                        await asyncio.sleep(min(0.25 * (2**retry), 1.0))
                        continue
                response.raise_for_status()
                result = response.json()
                logger.info(
                    "Reinvestigation dispatched for incident %s attempt %d",
                    incident_id,
                    attempt,
                )
                return result
            except (httpx.TimeoutException, httpx.TransportError):
                if retry == 2:
                    raise
                await asyncio.sleep(min(0.25 * (2**retry), 1.0))
            except httpx.HTTPStatusError as error:
                if error.response.status_code < 500 or retry == 2:
                    raise
                await asyncio.sleep(min(0.25 * (2**retry), 1.0))
        raise RuntimeError("Reinvestigation A2A dispatch attempts were exhausted")

    async def _synthesize(
        self, event: IncidentCreatedEvent, outcomes: dict[str, str] | None = None, reason: str = "initial"
    ) -> IncidentReport | None:
        """Fuse whatever is on the blackboard into the incident report (Week 4).

        Best effort by design: the dispatch has already completed and its evidence is
        safely on the blackboard, so a fusion error is logged and never re-raised. A
        partial blackboard still yields a report; ``findings`` says who contributed.
        """
        if self.adk_agent is None:
            return None
        try:
            # Fingerprint first: evidence landing after this point yields a later revision.
            fingerprint = evidence_fingerprint((await self.adk_agent.collect(event.incident_id)).evidence)
            report = await self.adk_agent.synthesize(event.incident_id, outcomes=outcomes)
        except Exception as error:  # noqa: BLE001 - must not fail a completed dispatch
            logger.error(
                "Coordinator synthesis failed (%s)",
                type(error).__name__,
                extra={"incident_id": str(event.incident_id)},
            )
            return None
        if report is None:
            return None
        self.reports[str(event.incident_id)] = report
        try:
            await self._record_revision(event, report, fingerprint, reason)
        except Exception as error:  # noqa: BLE001 - the report exists; persistence is repaired on restart
            logger.error(
                "Coordinator could not persist the report revision (%s)",
                type(error).__name__,
                extra={"incident_id": str(event.incident_id)},
            )
        logger.info(
            "Coordinator fused incident report",
            extra={
                "incident_id": str(event.incident_id),
                "status": report.status,
                "failure_type": report.failure_type.value,
                "confidence": report.confidence,
                "actionable": report.actionable,
                "reporting_agents": list(report.reporting_agents),
            },
        )
        return report

    async def stop(self) -> None:
        for task in list(self._late_tasks):
            task.cancel()
        if self.queue is not None and self.consumer_tag is not None:
            await self.queue.cancel(self.consumer_tag)
        if self.client is not None:
            await self.client.aclose()
        if self.connection is not None and not self.connection.is_closed:
            await self.connection.close()


runtime = CoordinatorRuntime()


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await runtime.start()
        yield
    finally:
        await runtime.stop()


app = FastAPI(title="JenkinsGuardian Coordinator", lifespan=lifespan)


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    active_runtime: CoordinatorRuntime = request.app.state.runtime
    if active_runtime.connection is None or active_runtime.connection.is_closed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Coordinator RabbitMQ connection is unavailable",
        )
    return {"status": "ok"}


app.state.runtime = runtime


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "9100")))
