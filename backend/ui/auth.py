"""Sign-in, sign-out, who-am-I, own password, and (admin) user management."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.db.audit_models import AuditEventType
from backend.db.database import get_db
from backend.db.ui_models import ROLES, User
from backend.ui.deps import audit, client_ip, current_user, require_role
from backend.ui.security import (
    hash_password,
    issue_session,
    limiter,
    normalize_email,
    password_problem,
    secret_configured,
    valid_email,
    verify_password,
)

router = APIRouter(tags=["ui-auth"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(Strict):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class PasswordChange(Strict):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


class NewUser(Strict):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)
    name: str = Field(default="", max_length=120)
    role: Literal["viewer", "reviewer", "admin"] = "viewer"


class UserPatch(Strict):
    name: str | None = Field(default=None, max_length=120)
    role: Literal["viewer", "reviewer", "admin"] | None = None
    active: bool | None = None
    password: str | None = Field(default=None, max_length=200)


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.UI_COOKIE_NAME,
        token,
        max_age=settings.UI_SESSION_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.UI_COOKIE_SECURE,
        path="/",
    )


def _session_payload(user: User, csrf: str) -> dict:
    return {"user": user.to_dict(), "csrf_token": csrf}


@router.post("/auth/login")
async def login(body: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> dict:
    if not secret_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Sign-in is not configured on this server")
    email, ip = normalize_email(body.email), client_ip(request)
    if limiter.blocked(ip, email):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many attempts. Try again later.")

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    password_ok = verify_password(body.password, user.password_hash if user else None)
    if user is None or not user.active or not password_ok:
        limiter.record_failure(ip, email)
        audit(db, email or "unknown", AuditEventType.USER_LOGIN_FAILED.value, "failed sign-in", {"ip": ip}, ok=False)
        await db.commit()
        # One message for every cause: no account enumeration.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    limiter.reset(ip, email)
    user.last_login_at = datetime.now(timezone.utc)
    audit(db, user.email, AuditEventType.USER_LOGIN.value, "signed in", {"ip": ip})
    await db.commit()
    token, csrf = issue_session(user.id, user.session_version)
    _set_cookie(response, token)
    return _session_payload(user, csrf)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> Response:
    audit(db, user.email, AuditEventType.USER_LOGOUT.value, "signed out")
    await db.commit()
    response.delete_cookie(settings.UI_COOKIE_NAME, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/auth/me")
async def me(request: Request, user: User = Depends(current_user)) -> dict:
    return _session_payload(user, request.state.csrf)


@router.post("/auth/password")
async def change_password(
    body: PasswordChange, response: Response, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Current password is incorrect")
    problem = password_problem(body.new_password, user.email)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
    user.password_hash = hash_password(body.new_password)
    user.session_version += 1  # every other session for this user ends
    audit(db, user.email, AuditEventType.USER_CHANGED.value, "changed own password")
    await db.commit()
    token, csrf = issue_session(user.id, user.session_version)
    _set_cookie(response, token)
    return _session_payload(user, csrf)


# ---- admin: user management --------------------------------------------------------------------


@router.get("/users")
async def list_users(_: User = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(User).order_by(User.email))).scalars().all()
    return [u.to_dict() for u in rows]


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(body: NewUser, admin: User = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)) -> dict:
    email = normalize_email(body.email)
    if not valid_email(email):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Enter a valid email address")
    problem = password_problem(body.password, email)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
    user = User(email=email, name=body.name.strip(), password_hash=hash_password(body.password), role=body.role, active=True)
    db.add(user)
    audit(db, admin.email, AuditEventType.USER_CHANGED.value, f"created user {email}", {"role": body.role})
    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="A user with this email already exists") from error
    return user.to_dict()


async def _active_admins(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(User).where(User.role == "admin", User.active.is_(True)))).scalar_one()


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int, body: UserPatch, admin: User = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)
) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")
    changes: dict = {}
    losing_admin = (body.role is not None and body.role != "admin" and user.role == "admin") or (
        body.active is False and user.role == "admin" and user.active
    )
    if losing_admin and await _active_admins(db) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This is the last active admin")
    if body.name is not None:
        user.name, changes["name"] = body.name.strip(), True
    if body.role is not None and body.role in ROLES:
        user.role, changes["role"] = body.role, body.role
    if body.active is not None:
        user.active, changes["active"] = body.active, body.active
        if not body.active:
            user.session_version += 1
    if body.password is not None:
        problem = password_problem(body.password, user.email)
        if problem:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
        user.password_hash, changes["password"] = hash_password(body.password), "reset"
        user.session_version += 1
    audit(db, admin.email, AuditEventType.USER_CHANGED.value, f"updated user {user.email}", {k: v for k, v in changes.items() if k != "password"} | ({"password": "reset"} if "password" in changes else {}))
    await db.commit()
    return user.to_dict()
