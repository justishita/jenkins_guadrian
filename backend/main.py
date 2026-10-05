import asyncio
from contextlib import asynccontextmanager
import logging
import sys
import uuid
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from backend.config import settings
from backend.db.database import init_db
from backend.events.publisher import publisher
from backend.routes.health import router as health_router
from backend.routes.webhooks import router as webhooks_router

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] [%(name)s] [req_id=%(request_id)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Custom log filter to inject request_id if missing
class RequestIdLogFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


root_logger = logging.getLogger()
for handler in root_logger.handlers:
    handler.addFilter(RequestIdLogFilter())

logger = logging.getLogger("backend.main")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Generates and tracks a correlation request ID for each incoming HTTP request."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id

        # Attach request_id to context
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown lifecycle manager."""
    logger.info("Starting up DevOps AI Agent API...")

    # 1. Initialize DB tables
    await init_db()

    # 2. Connect to RabbitMQ asynchronously with retry
    asyncio.create_task(publisher.connect(max_retries=5, retry_interval=2.0))

    yield

    # Shutdown
    logger.info("Shutting down DevOps AI Agent API...")
    await publisher.close()


app = FastAPI(
    title=settings.API_TITLE,
    version="1.0.0",
    lifespan=lifespan,
)

# Add middleware
app.add_middleware(RequestIdMiddleware)

# Include routers
app.include_router(health_router)
app.include_router(webhooks_router)


@app.get("/")
async def root():
    return {
        "service": settings.API_TITLE,
        "status": "running",
        "docs_url": "/docs",
    }