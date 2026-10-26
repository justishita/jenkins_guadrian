"""Accounts and projects for the web UI.

**Owner:** P2. A *project* registers what to watch - a Jenkins job (and everything under it, for
multibranch pipelines), the GitHub repository, and optionally the Prometheus ``job`` label. It does
not carry credentials: the agents keep using the one deployed set. An incident belongs to a project
when its ``job_name`` equals the project's job or starts with ``<job>/``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from backend.db.database import Base

ROLES = ("viewer", "reviewer", "admin")  # ascending privilege


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(254), unique=True, index=True, nullable=False)
    name = Column(String(120), nullable=False, default="")
    password_hash = Column(String(255), nullable=False)
    role = Column(String(16), nullable=False, default="viewer")
    active = Column(Boolean, nullable=False, default=True)
    # Bumped on password change or deactivation; sessions carry it, so they die with it.
    session_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        """Never includes the password hash."""
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "active": bool(self.active),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(80), unique=True, nullable=False)
    description = Column(Text, nullable=False, default="")
    jenkins_job = Column(String(255), nullable=False, index=True)
    github_repo = Column(String(140), nullable=False)
    prometheus_job = Column(String(80), nullable=True)
    archived = Column(Boolean, nullable=False, default=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "jenkins_job": self.jenkins_job,
            "github_repo": self.github_repo,
            "prometheus_job": self.prometheus_job,
            "archived": bool(self.archived),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ProjectMember(Base):
    """Who may see a project. Admins see every project without needing a row."""

    __tablename__ = "project_members"

    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)

    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_member"),)
