"""Notifiers and delivery pipeline for incident remediation approvals.

Provides:
- Notifier interface (ABC)
- SlackNotifier: incoming webhook
- TeamsNotifier: incoming webhook
- EmailNotifier: SMTP via aiosmtplib
- ConsoleNotifier: local dev fallback
- NotificationDispatcher: retries with exponential backoff on transient errors,
  falls through to the next channel when a provider is down, and records delivery status.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from datetime import datetime, timezone
from email.message import EmailMessage
import logging
import time
from typing import Any, Sequence

import aiosmtplib
import httpx
from pydantic import BaseModel, ConfigDict, Field

from backend.config import settings
from backend.notifications.renderer import RenderedNotification

logger = logging.getLogger(__name__)


class NotificationResult(BaseModel):
    """Result of an individual channel delivery attempt."""

    model_config = ConfigDict(frozen=True)

    channel: str
    success: bool
    error: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)


class DeliveryReport(BaseModel):
    """Consolidated delivery status recording all attempts and fallthroughs."""

    model_config = ConfigDict(frozen=True)

    incident_id: str
    attempt: int
    succeeded: bool
    final_channel: str | None
    results: list[NotificationResult]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Notifier(ABC):
    """Abstract interface for approval notification channels."""

    channel_name: str

    @abstractmethod
    async def send(self, notification: RenderedNotification) -> NotificationResult:
        """Send notification via this channel."""
        pass


class SlackNotifier(Notifier):
    """Sends notifications to Slack via incoming webhook."""

    channel_name = "slack"

    def __init__(self, webhook_url: str | None = None, timeout: float = 8.0) -> None:
        self.webhook_url = webhook_url if webhook_url is not None else settings.SLACK_WEBHOOK_URL
        self.timeout = timeout

    async def send(self, notification: RenderedNotification) -> NotificationResult:
        start = time.perf_counter()
        if not self.webhook_url:
            return NotificationResult(
                channel=self.channel_name,
                success=False,
                error="Slack webhook URL is not configured",
                duration_ms=0.0,
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.webhook_url,
                    json=notification.slack_payload,
                    headers={"Content-Type": "application/json"},
                )
                duration_ms = (time.perf_counter() - start) * 1000

                if response.status_code in (200, 204):
                    logger.info("Slack notification sent successfully for incident %s", notification.incident_id)
                    return NotificationResult(
                        channel=self.channel_name,
                        success=True,
                        duration_ms=duration_ms,
                        details={"status_code": response.status_code},
                    )

                error_msg = f"Slack webhook returned HTTP {response.status_code}: {response.text}"
                logger.warning(error_msg)
                return NotificationResult(
                    channel=self.channel_name,
                    success=False,
                    error=error_msg,
                    duration_ms=duration_ms,
                    details={"status_code": response.status_code},
                )
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            error_msg = f"Slack delivery failed ({type(exc).__name__}): {exc}"
            logger.warning(error_msg)
            return NotificationResult(
                channel=self.channel_name,
                success=False,
                error=error_msg,
                duration_ms=duration_ms,
            )


class TeamsNotifier(Notifier):
    """Sends notifications to Microsoft Teams via incoming webhook."""

    channel_name = "teams"

    def __init__(self, webhook_url: str | None = None, timeout: float = 8.0) -> None:
        self.webhook_url = webhook_url if webhook_url is not None else settings.TEAMS_WEBHOOK_URL
        self.timeout = timeout

    async def send(self, notification: RenderedNotification) -> NotificationResult:
        start = time.perf_counter()
        if not self.webhook_url:
            return NotificationResult(
                channel=self.channel_name,
                success=False,
                error="Teams webhook URL is not configured",
                duration_ms=0.0,
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.webhook_url,
                    json=notification.teams_payload,
                    headers={"Content-Type": "application/json"},
                )
                duration_ms = (time.perf_counter() - start) * 1000

                if response.status_code in (200, 201, 202):
                    logger.info("Teams notification sent successfully for incident %s", notification.incident_id)
                    return NotificationResult(
                        channel=self.channel_name,
                        success=True,
                        duration_ms=duration_ms,
                        details={"status_code": response.status_code},
                    )

                error_msg = f"Teams webhook returned HTTP {response.status_code}: {response.text}"
                logger.warning(error_msg)
                return NotificationResult(
                    channel=self.channel_name,
                    success=False,
                    error=error_msg,
                    duration_ms=duration_ms,
                    details={"status_code": response.status_code},
                )
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            error_msg = f"Teams delivery failed ({type(exc).__name__}): {exc}"
            logger.warning(error_msg)
            return NotificationResult(
                channel=self.channel_name,
                success=False,
                error=error_msg,
                duration_ms=duration_ms,
            )


class EmailNotifier(Notifier):
    """Sends email notifications via SMTP using aiosmtplib."""

    channel_name = "email"

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool | None = None,
        starttls: bool | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.host = host if host is not None else settings.SMTP_HOST
        self.port = port if port is not None else settings.SMTP_PORT
        self.username = username if username is not None else settings.SMTP_USER
        self.password = password if password is not None else settings.SMTP_PASSWORD
        self.use_tls = use_tls if use_tls is not None else settings.SMTP_USE_TLS
        self.starttls = starttls if starttls is not None else settings.SMTP_STARTTLS
        self.sender = sender if sender is not None else settings.NOTIFICATION_EMAIL_FROM
        self.recipient = recipient if recipient is not None else settings.NOTIFICATION_EMAIL_TO
        self.timeout = timeout

    async def send(self, notification: RenderedNotification) -> NotificationResult:
        start = time.perf_counter()
        if not self.recipient:
            return NotificationResult(
                channel=self.channel_name,
                success=False,
                error="Notification email recipient is not configured",
                duration_ms=0.0,
            )

        msg = EmailMessage()
        msg["Subject"] = notification.email_subject
        msg["From"] = self.sender
        msg["To"] = self.recipient
        msg.set_content(notification.email_text)
        msg.add_alternative(notification.email_html, subtype="html")

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.host,
                port=self.port,
                username=self.username or None,
                password=self.password or None,
                use_tls=self.use_tls,
                start_tls=self.starttls,
                timeout=self.timeout,
            )
            duration_ms = (time.perf_counter() - start) * 1000
            logger.info("Email notification sent successfully for incident %s", notification.incident_id)
            return NotificationResult(
                channel=self.channel_name,
                success=True,
                duration_ms=duration_ms,
                details={"recipient": self.recipient, "host": self.host, "port": self.port},
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            error_msg = f"SMTP delivery failed ({type(exc).__name__}): {exc}"
            logger.warning(error_msg)
            return NotificationResult(
                channel=self.channel_name,
                success=False,
                error=error_msg,
                duration_ms=duration_ms,
            )


class ConsoleNotifier(Notifier):
    """Development fallback notifier that outputs structured messages to logs/console."""

    channel_name = "console"

    async def send(self, notification: RenderedNotification) -> NotificationResult:
        start = time.perf_counter()
        # Log clearly for local development
        logger.info(
            "\n%s\n[FALLBACK CONSOLE NOTIFICATION]\n%s\n%s",
            "=" * 60,
            notification.plain_text,
            "=" * 60,
        )
        duration_ms = (time.perf_counter() - start) * 1000
        return NotificationResult(
            channel=self.channel_name,
            success=True,
            duration_ms=duration_ms,
            details={"console_output": True},
        )


class NotificationDispatcher:
    """Manages channel priority, retries with backoff, and fallthrough when providers fail."""

    def __init__(
        self,
        channels: Sequence[Notifier] | None = None,
        max_retries: int | None = None,
        base_backoff: float | None = None,
    ) -> None:
        self.channels = list(channels) if channels is not None else [
            SlackNotifier(),
            TeamsNotifier(),
            EmailNotifier(),
            ConsoleNotifier(),
        ]
        self.max_retries = max_retries if max_retries is not None else settings.NOTIFICATION_MAX_RETRIES
        self.base_backoff = base_backoff if base_backoff is not None else settings.NOTIFICATION_RETRY_BACKOFF

    async def dispatch(self, notification: RenderedNotification) -> DeliveryReport:
        """Attempt sending across channels in order.

        If a channel fails (e.g. provider down or transient error), retries with backoff.
        If all retries for that channel fail, falls through to the next channel in sequence.
        Returns a DeliveryReport recording all attempts.
        """
        all_results: list[NotificationResult] = []

        for channel in self.channels:
            logger.info("Attempting delivery via channel '%s' for incident %s", channel.channel_name, notification.incident_id)

            channel_succeeded = False
            last_result: NotificationResult | None = None

            for attempt_idx in range(1, self.max_retries + 1):
                res = await channel.send(notification)
                last_result = res
                all_results.append(res)

                if res.success:
                    channel_succeeded = True
                    break

                # Provider failed or error: if we have more retries, sleep with bounded exponential backoff
                if attempt_idx < self.max_retries:
                    backoff = min(self.base_backoff * (2 ** (attempt_idx - 1)), 8.0)
                    logger.warning(
                        "Channel '%s' attempt %d/%d failed (%s); retrying in %.2fs",
                        channel.channel_name,
                        attempt_idx,
                        self.max_retries,
                        res.error,
                        backoff,
                    )
                    await asyncio.sleep(backoff)

            if channel_succeeded:
                logger.info(
                    "Notification successfully delivered via '%s' for incident %s",
                    channel.channel_name,
                    notification.incident_id,
                )
                return DeliveryReport(
                    incident_id=notification.incident_id,
                    attempt=notification.attempt,
                    succeeded=True,
                    final_channel=channel.channel_name,
                    results=all_results,
                )

            logger.warning(
                "Channel '%s' failed all %d attempts; falling through to next notification channel",
                channel.channel_name,
                self.max_retries,
            )

        # If all channels (including fallbacks) failed:
        logger.error("All notification channels failed for incident %s", notification.incident_id)
        return DeliveryReport(
            incident_id=notification.incident_id,
            attempt=notification.attempt,
            succeeded=False,
            final_channel=None,
            results=all_results,
        )


# Default dispatcher instance
dispatcher = NotificationDispatcher()
