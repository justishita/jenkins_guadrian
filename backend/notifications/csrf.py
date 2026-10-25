"""CSRF protection utilities for server-rendered HTML approval pages.

Uses HMAC-SHA256 tokens tied to incident_id and timestamp to ensure
browser-submitted approval decisions cannot be forged.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import logging
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException, Request, status

from backend.config import settings

logger = logging.getLogger(__name__)

CSRF_EXPIRY_SECONDS = 24 * 3600  # 24 hours


def _get_csrf_secret() -> bytes:
    key = settings.APPROVAL_SIGNING_SECRET or settings.WEBHOOK_SHARED_SECRET or "dev-insecure-csrf-secret"
    return key.encode("utf-8")


def generate_csrf_token(incident_id: str, *, nonce: Optional[str] = None) -> str:
    """Generate a signed, expiring CSRF token for an incident review session."""
    now = int(datetime.now(timezone.utc).timestamp())
    n = nonce or uuid4().hex
    raw = f"{incident_id}:{n}:{now}"
    signature = hmac.new(_get_csrf_secret(), raw.encode("utf-8"), hashlib.sha256).digest()
    b64_sig = base64.urlsafe_b64encode(signature).decode("utf-8").rstrip("=")
    b64_raw = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"{b64_raw}.{b64_sig}"


def verify_csrf_token(token: str, incident_id: str) -> bool:
    """Verify constant-time signature and expiration of a CSRF token."""
    if not token or "." not in token:
        return False

    parts = token.split(".")
    if len(parts) != 2:
        return False

    b64_raw, b64_sig = parts
    try:
        # Pad base64 if needed
        pad_raw = 4 - (len(b64_raw) % 4)
        raw_bytes = base64.urlsafe_b64decode(b64_raw + ("=" * (pad_raw % 4)))
        raw_str = raw_bytes.decode("utf-8")

        pad_sig = 4 - (len(b64_sig) % 4)
        received_sig = base64.urlsafe_b64decode(b64_sig + ("=" * (pad_sig % 4)))
    except Exception:
        return False

    # Check HMAC signature
    expected_sig = hmac.new(_get_csrf_secret(), raw_str.encode("utf-8"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected_sig, received_sig):
        logger.warning("CSRF token signature mismatch for incident %s", incident_id)
        return False

    # Check incident match and expiration
    try:
        token_inc_id, _, ts_str = raw_str.split(":")
        if token_inc_id != str(incident_id):
            logger.warning("CSRF incident mismatch: expected %s, got %s", incident_id, token_inc_id)
            return False
        timestamp = int(ts_str)
        now = int(datetime.now(timezone.utc).timestamp())
        if now - timestamp > CSRF_EXPIRY_SECONDS:
            logger.warning("CSRF token expired for incident %s", incident_id)
            return False
    except Exception:
        return False

    return True
