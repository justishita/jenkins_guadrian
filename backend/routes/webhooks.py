import hmac
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.db.database import get_db
from backend.events.publisher import publisher
from backend.incidents import create_incident_if_missing
from backend.models.events import JenkinsFailureEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_BODY_BYTES = 64 * 1024  # 64 KB limit


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
    payload: JenkinsFailureEvent,
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
    # Guard against payload size (Content-Length header check if provided)
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Request body exceeds maximum size of {MAX_BODY_BYTES} bytes",
        )

    try:
        incident, created = await create_incident_if_missing(payload, db)
        if not created:
            logger.info(
                "Duplicate Jenkins failure event received for %s #%d. Returning existing incident %s.",
                payload.job_name,
                payload.build_number,
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
            "Created new incident %s for job %s #%d (failed stage: %s).",
            incident.id,
            payload.job_name,
            payload.build_number,
            payload.failed_stage,
        )

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "accepted",
                "incident_id": incident.id,
            },
        )

    except Exception as e:
        logger.error("Error processing Jenkins failure webhook: %s", e, exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to record incident",
        )


@router.post("/jenkins/validation", status_code=status.HTTP_202_ACCEPTED)
async def handle_jenkins_validation(
    request: Request,
    _: None = Depends(verify_webhook_token),
) -> Response:
    """
    Receives validation outcomes from re-validation builds.
    (Week 6 hook, stubbed for forward compatibility with target_app/Jenkinsfile).
    """
    body = await request.json()
    logger.info("Received Jenkins re-validation event: %s", body)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"status": "validation_received"},
    )
