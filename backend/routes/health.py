from fastapi import APIRouter, Response, status
from backend.db.database import check_db_health
from backend.events.publisher import publisher
from backend.models.events import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def get_health(response: Response) -> HealthResponse:
    """Returns connectivity status of PostgreSQL and RabbitMQ."""
    db_ok = await check_db_health()
    rmq_ok = await publisher.is_healthy()

    pg_status = "healthy" if db_ok else "unhealthy"
    rmq_status = "healthy" if rmq_ok else "unhealthy"

    overall_status = "ok" if (db_ok and rmq_ok) else "degraded"

    if overall_status == "degraded":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status=overall_status,
        postgres=pg_status,
        rabbitmq=rmq_status,
    )
