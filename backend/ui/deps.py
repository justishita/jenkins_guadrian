"""FastAPI dependencies shared by the UI routes: who is calling, with which role, and the audit helper."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.db.audit_models import AuditEvent
from backend.db.database import get_db
from backend.db.ui_models import Project, ProjectMember, User
from backend.ui.security import csrf_matches, read_session

ROLE_ORDER = {"viewer": 0, "reviewer": 1, "admin": 2}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
#: ``audit_events.incident_id`` is required, so events that belong to no incident (logins, project
#: changes) use this all-zero id. Dashboard variables and timelines exclude it.
SYSTEM_INCIDENT = "00000000-0000-0000-0000-000000000000"


def client_ip(request: Request) -> str:
    """The caller's address. Behind our nginx the real one arrives in ``X-Real-IP``."""
    if settings.UI_TRUST_PROXY:
        forwarded = request.headers.get("x-real-ip")
        if forwarded:
            return forwarded.strip()[:64]
    return request.client.host if request.client else "unknown"


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """The signed-in, active user; enforces the CSRF header on every state-changing request."""
    session = read_session(request.cookies.get(settings.UI_COOKIE_NAME))
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
    user = await db.get(User, session.user_id)
    if user is None or not user.active or user.session_version != session.version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
    if request.method not in SAFE_METHODS and not csrf_matches(session.csrf, request.headers.get("x-csrf-token")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Missing or invalid CSRF token")
    request.state.csrf = session.csrf
    return user


def require_role(minimum: str):
    """Dependency factory: the caller must hold at least ``minimum``."""

    async def dependency(user: User = Depends(current_user)) -> User:
        if ROLE_ORDER.get(user.role, -1) < ROLE_ORDER[minimum]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"Requires the {minimum} role")
        return user

    return dependency


async def project_for(db: AsyncSession, user: User, project_id: int) -> Project:
    """The project, if this user may see it. A project they may not see is a 404, never a 403."""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    if user.role != "admin":
        membership = (
            await db.execute(
                select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user.id)
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


async def visible_projects(db: AsyncSession, user: User, *, include_archived: bool = False) -> list[Project]:
    query = select(Project).order_by(Project.name)
    if not include_archived:
        query = query.where(Project.archived.is_(False))
    if user.role != "admin":
        query = query.join(ProjectMember, ProjectMember.project_id == Project.id).where(ProjectMember.user_id == user.id)
    return list((await db.execute(query)).scalars().all())


def audit(
    db: AsyncSession, actor: str, event_type: str, summary: str, payload: dict[str, Any] | None = None, *, ok: bool = True
) -> None:
    """Queue an audit row on the caller's transaction (the caller commits)."""
    db.add(
        AuditEvent(
            incident_id=SYSTEM_INCIDENT,
            actor=actor[:64],
            event_type=event_type,
            summary=summary,
            payload=payload or {},
            ok=ok,
        )
    )
