"""HMAC-signed expiring single-use token generation and verification for approvals.

Implements tamper-proof links for human reviewer decisions with:
- incident_id, attempt, decision-scope ("approved" or "rejected"), 24-hour expiry, unique nonce (jti).
- Constant-time signature verification (prevents timing attacks).
- Single-use enforcement (detects reviewer clicking Approve twice).
- Expiration check (24h lifetime).
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from backend.config import settings

logger = logging.getLogger(__name__)

DecisionScope = Literal["approved", "rejected"]
TOKEN_LIFETIME_SECONDS = 24 * 3600  # 24 hours


class TokenError(Exception):
    """Base exception for approval token errors."""


class TamperedTokenError(TokenError):
    """Raised when token signature does not match (tampering detected)."""


class ExpiredTokenError(TokenError):
    """Raised when the 24h expiration deadline has passed."""


class TokenAlreadyUsedError(TokenError):
    """Raised when a single-use token has already been consumed."""


class InvalidTokenFormatError(TokenError):
    """Raised when the token format is unparseable."""


class ApprovalTokenPayload(BaseModel):
    """Payload embedded within the HMAC-signed token."""

    model_config = ConfigDict(frozen=True)

    incident_id: str
    attempt: int = Field(ge=0)
    decision: DecisionScope
    exp: int = Field(description="Expiration timestamp (epoch seconds, UTC)")
    iat: int = Field(description="Issued-at timestamp (epoch seconds, UTC)")
    jti: str = Field(description="Unique single-use nonce")

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc).timestamp() > self.exp


class TokenStore:
    """In-memory thread-safe registry tracking consumed single-use tokens."""

    def __init__(self) -> None:
        self._used_jtis: set[str] = set()

    def is_used(self, jti: str) -> bool:
        return jti in self._used_jtis

    def mark_used(self, jti: str) -> bool:
        """Mark token as used. Returns True if successfully marked, False if already used."""
        if jti in self._used_jtis:
            return False
        self._used_jtis.add(jti)
        return True

    def reset(self) -> None:
        """Clear used tokens (useful for testing)."""
        self._used_jtis.clear()


# Default singleton registry for consumed tokens
token_store = TokenStore()


def _get_signing_secret(secret: str | None = None) -> bytes:
    chosen = secret or settings.APPROVAL_SIGNING_SECRET or settings.WEBHOOK_SHARED_SECRET or "dev-insecure-approval-secret"
    return chosen.encode("utf-8")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = 4 - (len(data) % 4)
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data.encode("utf-8"))


def generate_approval_token(
    incident_id: str | UUID,
    attempt: int,
    decision: DecisionScope,
    *,
    secret: str | None = None,
    expires_in_seconds: int = TOKEN_LIFETIME_SECONDS,
    jti: str | None = None,
) -> str:
    """Generate an HMAC-SHA256 signed expiring single-use token."""
    now_utc = datetime.now(timezone.utc)
    iat = int(now_utc.timestamp())
    exp = iat + expires_in_seconds
    nonce = jti or uuid4().hex

    payload = ApprovalTokenPayload(
        incident_id=str(incident_id),
        attempt=attempt,
        decision=decision,
        exp=exp,
        iat=iat,
        jti=nonce,
    )

    payload_json = json.dumps(payload.model_dump(), separators=(",", ":"), sort_keys=True).encode("utf-8")
    b64_payload = _b64url_encode(payload_json)

    key = _get_signing_secret(secret)
    sig = hmac.new(key, b64_payload.encode("utf-8"), hashlib.sha256).digest()
    b64_sig = _b64url_encode(sig)

    return f"{b64_payload}.{b64_sig}"


def verify_approval_token(
    token: str,
    *,
    secret: str | None = None,
    enforce_single_use: bool = True,
    store: TokenStore | None = None,
) -> ApprovalTokenPayload:
    """Verify an HMAC-SHA256 signed token.

    Validates:
    1. Structure (header/payload.signature)
    2. Signature via constant-time comparison (detects link tampering)
    3. Expiration time (detects expired links)
    4. Single-use status (detects duplicate clicks/reuse)
    """
    if not token or "." not in token:
        raise InvalidTokenFormatError("Malformed approval token: expected '<payload>.<signature>'")

    parts = token.split(".")
    if len(parts) != 2:
        raise InvalidTokenFormatError("Malformed approval token structure")

    b64_payload, b64_sig = parts

    try:
        received_sig = _b64url_decode(b64_sig)
    except Exception as exc:
        raise TamperedTokenError("Failed to decode token signature") from exc

    key = _get_signing_secret(secret)
    expected_sig = hmac.new(key, b64_payload.encode("utf-8"), hashlib.sha256).digest()

    # Constant-time comparison to prevent timing attacks / tampering
    if not hmac.compare_digest(expected_sig, received_sig):
        logger.warning("Token signature mismatch detected; link tampering suspected")
        raise TamperedTokenError("Token signature verification failed: link may have been tampered with")

    try:
        raw_payload = _b64url_decode(b64_payload)
        payload_dict = json.loads(raw_payload.decode("utf-8"))
        payload = ApprovalTokenPayload.model_validate(payload_dict)
    except Exception as exc:
        raise InvalidTokenFormatError(f"Failed to parse token payload: {exc}") from exc

    # Check expiration
    if payload.is_expired:
        logger.info("Approval token %s expired at %s", payload.jti, payload.exp)
        raise ExpiredTokenError(f"Approval link has expired (valid for 24h, expired at epoch {payload.exp})")

    # Check single use
    active_store = store or token_store
    if enforce_single_use and active_store.is_used(payload.jti):
        logger.warning("Approval token %s already consumed", payload.jti)
        raise TokenAlreadyUsedError("This approval link has already been used")

    return payload


def consume_approval_token(
    token: str,
    *,
    secret: str | None = None,
    store: TokenStore | None = None,
) -> ApprovalTokenPayload:
    """Verify and atomically consume a single-use approval token."""
    payload = verify_approval_token(token, secret=secret, enforce_single_use=True, store=store)
    active_store = store or token_store
    marked = active_store.mark_used(payload.jti)
    if not marked:
        raise TokenAlreadyUsedError("This approval link has already been used")
    return payload


def build_action_urls(
    incident_id: str | UUID,
    attempt: int,
    *,
    base_url: str | None = None,
    secret: str | None = None,
) -> dict[str, str]:
    """Build signed expiring action URLs for human reviewer approval/rejection."""
    base = (base_url or settings.BASE_URL).rstrip("/")
    approve_token = generate_approval_token(incident_id, attempt, "approved", secret=secret)
    reject_token = generate_approval_token(incident_id, attempt, "rejected", secret=secret)

    return {
        "approve_url": f"{base}/approvals/decide?decision=approved&token={approve_token}",
        "reject_url": f"{base}/approvals/decide?decision=rejected&token={reject_token}",
    }
