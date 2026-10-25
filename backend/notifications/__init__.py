"""Backend notifications package.

Provides:
- Notifier: Base notifier interface
- SlackNotifier: Slack incoming webhook integration
- TeamsNotifier: Microsoft Teams incoming webhook integration
- EmailNotifier: SMTP integration via aiosmtplib
- ConsoleNotifier: Development fallback
- NotificationDispatcher: Fallback priority dispatcher with exponential retry
- NotificationRenderer: Message rendering with secrets redaction & length limits
- ApprovalRequestedConsumer: RabbitMQ consumer of 'approval.requested'
- Tokens: HMAC-signed, expiring, single-use action tokens
"""

from backend.notifications.consumer import ApprovalRequestedConsumer, approval_consumer
from backend.notifications.notifier import (
    ConsoleNotifier,
    DeliveryReport,
    EmailNotifier,
    NotificationDispatcher,
    NotificationResult,
    Notifier,
    SlackNotifier,
    TeamsNotifier,
    dispatcher,
)
from backend.notifications.renderer import (
    NotificationRenderer,
    RenderedNotification,
    TopEvidenceItem,
    renderer,
)
from backend.notifications.tokens import (
    ApprovalTokenPayload,
    ExpiredTokenError,
    InvalidTokenFormatError,
    TamperedTokenError,
    TokenAlreadyUsedError,
    TokenError,
    TokenStore,
    build_action_urls,
    consume_approval_token,
    generate_approval_token,
    token_store,
    verify_approval_token,
)

__all__ = [
    "ApprovalRequestedConsumer",
    "ApprovalTokenPayload",
    "ConsoleNotifier",
    "DeliveryReport",
    "EmailNotifier",
    "ExpiredTokenError",
    "InvalidTokenFormatError",
    "NotificationDispatcher",
    "NotificationRenderer",
    "NotificationResult",
    "Notifier",
    "RenderedNotification",
    "SlackNotifier",
    "TamperedTokenError",
    "TeamsNotifier",
    "TokenAlreadyUsedError",
    "TokenError",
    "TokenStore",
    "TopEvidenceItem",
    "approval_consumer",
    "build_action_urls",
    "consume_approval_token",
    "dispatcher",
    "generate_approval_token",
    "renderer",
    "token_store",
    "verify_approval_token",
]
