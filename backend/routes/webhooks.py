import hmac
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.db.database import get_db
from backend.incidents import create_incident_if_missing
from backend.models.events import JenkinsFailureEvent
from common.redaction import redact_value

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_BODY_BYTES = 64 * 1024  # 64 KB limit


async def _read_limited_body(request: Request) -> bytes:
    """Read request bytes with a hard cap, regardless of Content-Length."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length header",
            ) from error
        if declared_length < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length header",
            )
        if declared_length > MAX_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"Request body exceeds maximum size of {MAX_BODY_BYTES} bytes",
            )

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"Request body exceeds maximum size of {MAX_BODY_BYTES} bytes",
            )
    return bytes(body)


def verify_webhook_token(x_webhook_token: Optional[str] = Header(None)) -> None:
    """Validate webhook secret token using constant-time comparison."""
    expected_secret = settings.WEBHOOK_SHARED_SECRET
    if not expected_secret or not x_webhook_token:
        logger.warning("Webhook authentication failed: missing token or secret not configured.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or unauthorized webhook token",
        )

    if not hmac.compare_digest(x_webhook_token, expected_secret):
        logger.warning("Webhook authentication failed: invalid token provided.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook token",
        )


@router.post("/jenkins", status_code=status.HTTP_202_ACCEPTED)
async def handle_jenkins_failure(
    request: Request,
    _: None = Depends(verify_webhook_token),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Receives failure notifications from Jenkins builds.
    - Constant-time secret authentication
    - Enforces 64KB body limit
    - Computes event_key = sha256(job_name|build_number|build_url) for idempotency
    - Persists incident row to PostgreSQL
    - Publishes incident.created to RabbitMQ exchange
    - Returns 202 Accepted in < 200ms
    """
    body = await _read_limited_body(request)
    try:
        parsed = JenkinsFailureEvent.model_validate_json(body)
        payload = JenkinsFailureEvent.model_validate(
            redact_value(parsed.model_dump(mode="json"))
        )
    except ValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid Jenkins failure payload",
        ) from error

    try:
        incident, created = await create_incident_if_missing(payload, db)
        if not created:
            logger.info(
                "Duplicate Jenkins failure event received for incident %s.",
                incident.id,
            )
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "status": "already_processed",
                    "incident_id": incident.id,
                    "message": "Incident already recorded for this build",
                },
            )

        logger.info(
            "Created Jenkins incident %s.",
            incident.id,
        )

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "accepted",
                "incident_id": incident.id,
            },
        )

    except Exception as error:
        logger.error(
            "Error processing Jenkins failure webhook (%s)",
            type(error).__name__,
        )
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to record incident",
        ) from error


# Note: /jenkins/validation is implemented in backend/routes/validation.py
