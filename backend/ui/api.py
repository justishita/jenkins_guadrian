"""The UI's JSON API, mounted at ``/api/v1``."""

from __future__ import annotations

from fastapi import APIRouter

from backend.config import settings
from backend.ui import auth, incidents, projects, stats

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(projects.router)
router.include_router(incidents.router)
router.include_router(stats.router)


@router.get("/config", tags=["ui-config"])
async def public_config() -> dict[str, str | bool]:
    """Non-secret settings the front end needs before sign-in."""
    return {"grafana_url": settings.GRAFANA_PUBLIC_URL, "signin_configured": len(settings.UI_SESSION_SECRET) >= 32}
