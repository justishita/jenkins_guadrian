"""RabbitMQ consumer for 'approval.requested' events.

Listens for approval.requested events, renders formatted multi-channel notifications
with signed action URLs, and dispatches via configured notifiers with fallback.

Handles:
- Deduplication by (incident_id, attempt)
- Secrets redaction on all payloads
- Provider fallthrough and delivery status auditing
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

import aio_pika
from sqlalchemy import select

from backend.config import settings
from backend.db.audit_models import AuditEvent
from backend.db.database import AsyncSessionLocal
from backend.db.models import Incident
from backend.notifications.notifier import DeliveryReport, NotificationDispatcher, dispatcher as default_dispatcher
from backend.notifications.renderer import NotificationRenderer, RenderedNotification, renderer as default_renderer
from common.evidence_store import FileEvidenceStore
from common.redaction import redact_value

logger = logging.getLogger(__name__)


class ApprovalRequestedConsumer:
    """Consumes 'approval.requested' events and triggers approval notifications."""

    def __init__(
        self,
        rabbitmq_url: str | None = None,
        exchange_name: str = "incidents",
        queue_name: str = "notifications.approval.requested",
        notification_dispatcher: NotificationDispatcher | None = None,
        notification_renderer: NotificationRenderer | None = None,
        evidence_dir: Path | None = None,
    ) -> None:
        self.rabbitmq_url = rabbitmq_url or settings.RABBITMQ_URL
        self.exchange_name = exchange_name
        self.queue_name = queue_name
        self.dispatcher = notification_dispatcher or default_dispatcher
        self.renderer = notification_renderer or default_renderer
        self.evidence_dir = evidence_dir or Path("data/evidence")

        self.connection: Optional[aio_pika.abc.AbstractRobustConnection] = None
        self.channel: Optional[aio_pika.abc.AbstractChannel] = None
        self.queue: Optional[aio_pika.abc.AbstractQueue] = None
        self.consumer_tag: Optional[str] = None
        self._lock = asyncio.Lock()

        # In-memory deduplication set: (incident_id, attempt)
        self._processed_approvals: set[tuple[str, int]] = set()

    def is_duplicate(self, incident_id: str, attempt: int) -> bool:
        """Check if this incident and attempt has already been processed."""
        return (str(incident_id), int(attempt)) in self._processed_approvals

    def mark_processed(self, incident_id: str, attempt: int) -> None:
        """Record (incident_id, attempt) as processed to deduplicate subsequent events."""
        self._processed_approvals.add((str(incident_id), int(attempt)))

    def reset_dedup(self) -> None:
        """Reset deduplication state (useful for tests)."""
        self._processed_approvals.clear()

    async def connect(self, max_retries: int = 5, retry_interval: float = 2.0) -> bool:
        """Connect to RabbitMQ and declare exchange, queue, and bindings."""
        async with self._lock:
            if self.connection and not self.connection.is_closed:
                return True

            for attempt in range(1, max_retries + 1):
                try:
                    logger.info("Notification consumer connecting to RabbitMQ (%d/%d)...", attempt, max_retries)
                    self.connection = await aio_pika.connect_robust(self.rabbitmq_url)
                    self.channel = await self.connection.channel()
                    await self.channel.set_qos(prefetch_count=1)

                    exchange = await self.channel.declare_exchange(
                        self.exchange_name,
                        type=aio_pika.ExchangeType.TOPIC,
                        durable=True,
                    )
                    self.queue = await self.channel.declare_queue(self.queue_name, durable=True)
                    await self.queue.bind(exchange, routing_key="approval.requested")

                    self.consumer_tag = await self.queue.consume(self.process_message)
                    logger.info("Notification consumer bound to 'approval.requested' on '%s'", self.exchange_name)
                    return True
                except Exception as exc:
                    logger.warning("RabbitMQ notification consumer connection failed: %s", exc)
                    if attempt < max_retries:
                        await asyncio.sleep(min(retry_interval * (2 ** (attempt - 1)), 8.0))
            return False

    async def close(self) -> None:
        """Gracefully close the consumer."""
        async with self._lock:
            if self.channel and not self.channel.is_closed and self.consumer_tag:
                try:
                    await self.queue.cancel(self.consumer_tag)
                except Exception:
                    pass
            if self.connection and not self.connection.is_closed:
                await self.connection.close()

    async def _resolve_incident(self, incident_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Fetch incident details from payload or load from database."""
        if "incident" in payload and isinstance(payload["incident"], dict):
            return payload["incident"]

        try:
            async with AsyncSessionLocal() as session:
                stmt = select(Incident).where(Incident.id == incident_id)
                res = await session.execute(stmt)
                incident = res.scalar_one_or_none()
                if incident:
                    return incident.to_dict()
        except Exception as exc:
            logger.warning("Could not query DB for incident %s: %s", incident_id, exc)

        # Fallback to values in payload
        return {
            "id": incident_id,
            "job_name": payload.get("job_name", "unknown-job"),
            "build_number": payload.get("build_number", 0),
            "build_url": payload.get("build_url", ""),
            "branch": payload.get("branch", "unknown-branch"),
            "failed_stage": payload.get("failed_stage", "unknown-stage"),
        }

    async def _resolve_findings(self, incident_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Fetch Jenkins findings from payload or load from evidence store."""
        if "jenkins_findings" in payload and isinstance(payload["jenkins_findings"], dict):
            return payload["jenkins_findings"]
        if "evidence" in payload and isinstance(payload["evidence"], dict):
            return payload["evidence"]

        # Try to read from FileEvidenceStore
        try:
            store = FileEvidenceStore(self.evidence_dir)
            evidence = await store.read(UUID(incident_id), "jenkins_agent")
            if evidence:
                return evidence.model_dump()
        except Exception as exc:
            logger.debug("Could not read evidence store for incident %s: %s", incident_id, exc)

        return {
            "failure_type": payload.get("failure_type", "unknown"),
            "confidence": payload.get("confidence", 0.0),
            "summary": payload.get("summary", "Failure requiring human approval"),
            "evidence_items": payload.get("evidence_items", []),
        }

    async def handle_approval_request(self, payload: dict[str, Any]) -> DeliveryReport | None:
        """Core handler for an approval requested event.

        Performs:
        1. Redaction of input payload
        2. Deduplication check by (incident_id, attempt)
        3. Information synthesis (incident + Jenkins findings + Coordinator proposed fix)
        4. Notification rendering with signed URLs
        5. Multi-channel dispatch with retry and fallback
        6. Audit record logging
        """
        # Redact raw inputs first
        safe_payload = redact_value(payload)

        incident_id = str(safe_payload.get("incident_id") or "")
        if not incident_id:
            logger.error("Received approval.requested event missing incident_id: %s", safe_payload)
            return None

        attempt = int(safe_payload.get("attempt") or safe_payload.get("remediation_attempt") or 0)

        # Edge case: Deduplicate by incident_id + attempt
        if self.is_duplicate(incident_id, attempt):
            logger.info(
                "Duplicate approval.requested event ignored for incident %s (attempt %d)",
                incident_id,
                attempt,
            )
            return None

        logger.info("Processing approval.requested for incident %s (attempt %d)", incident_id, attempt)

        # Fetch incident and findings
        incident_data = await self._resolve_incident(incident_id, safe_payload)
        findings_data = await self._resolve_findings(incident_id, safe_payload)

        proposed_fix = (
            safe_payload.get("proposed_fix")
            or safe_payload.get("remediation_proposal")
            or safe_payload.get("plan")
            or safe_payload.get("proposal")
            or {
                "rationale": safe_payload.get("rationale", "Proposed automated fix for review"),
                "diff": safe_payload.get("diff", ""),
                "changed_files": safe_payload.get("changed_files", []),
            }
        )

        # Render notification
        rendered = self.renderer.render(
            incident=incident_data,
            jenkins_findings=findings_data,
            proposed_fix=proposed_fix,
            attempt=attempt,
        )

        # Dispatch across channels (handles retry with backoff, fallthrough to next channel)
        delivery_report = await self.dispatcher.dispatch(rendered)

        # Mark processed for deduplication
        self.mark_processed(incident_id, attempt)

        # Record delivery status in DB / audit trail
        await self._record_delivery_audit(delivery_report)

        return delivery_report

    async def _record_delivery_audit(self, report: DeliveryReport) -> None:
        """Persist notification delivery status to the audit log."""
        try:
            async with AsyncSessionLocal() as session:
                audit_event = AuditEvent(
                    incident_id=report.incident_id,
                    actor="notifier",
                    event_type="approval_notification_dispatched",
                    summary=(
                        f"Approval notification sent via {report.final_channel}"
                        if report.succeeded
                        else "Failed to deliver approval notification on all channels"
                    ),
                    payload={
                        "attempt": report.attempt,
                        "succeeded": report.succeeded,
                        "final_channel": report.final_channel,
                        "delivery_attempts": [r.model_dump(mode="json") for r in report.results],
                    },
                    ok=report.succeeded,
                )
                session.add(audit_event)
                await session.commit()
                logger.info(
                    "Recorded notification audit event for incident %s (status: %s)",
                    report.incident_id,
                    "success" if report.succeeded else "failure",
                )
        except Exception as exc:
            logger.warning("Failed to record notification audit event to DB: %s", exc)

    async def process_message(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        """aio-pika incoming message callback."""
        try:
            async with message.process(requeue=False):
                body = json.loads(message.body.decode("utf-8"))
                await self.handle_approval_request(body)
        except Exception as exc:
            logger.error("Error processing approval.requested message: %s", exc, exc_info=True)


# Default consumer instance
approval_consumer = ApprovalRequestedConsumer()
