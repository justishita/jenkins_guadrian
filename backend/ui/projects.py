"""Projects: register what to watch, and who may see it."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.audit_models import AuditEventType, ProposalStatus, RemediationProposal
from backend.db.database import get_db
from backend.db.models import Incident
from backend.db.ui_models import Project, ProjectMember, User
from backend.ui.deps import audit, current_user, project_for, require_role, visible_projects

router = APIRouter(tags=["ui-projects"])

_JOB = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,100}(/[A-Za-z0-9][A-Za-z0-9._ -]{0,100}){0,4}$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,79}$")


class ProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=500)
    jenkins_job: str = Field(min_length=1, max_length=255)
    github_repo: str = Field(min_length=3, max_length=140)
    prometheus_job: str | None = Field(default=None, max_length=80)

    @field_validator("name", "description")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("jenkins_job")
    @classmethod
    def _job(cls, value: str) -> str:
        value = value.strip().strip("/")
        if not _JOB.match(value):
            raise ValueError("Jenkins job must look like 'my-app' or 'folder/my-app' (letters, digits, . _ - and spaces)")
        return value

    @field_validator("github_repo")
    @classmethod
    def _repo(cls, value: str) -> str:
        value = value.strip().strip("/")
        if not _REPO.match(value):
            raise ValueError("GitHub repository must be in 'owner/name' form, not a URL")
        return value

    @field_validator("prometheus_job")
    @classmethod
    def _label(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if not value:
            return None
        if not _LABEL.match(value):
            raise ValueError("Prometheus job label may only contain letters, digits, _ and -")
        return value


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    prometheus_job: str | None = Field(default=None, max_length=80)
    archived: bool | None = None


class MembersBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_ids: list[int] = Field(max_length=500)


def job_matches(project_job: str, incident_job: str) -> bool:
    """An incident belongs to a project when its job is the project's job or sits under it."""
    return incident_job == project_job or incident_job.startswith(project_job + "/")


def incidents_of(project: Project):
    """A SQLAlchemy condition selecting this project's incidents (``%``/``_`` in names are escaped)."""
    return (Incident.job_name == project.jenkins_job) | Incident.job_name.startswith(project.jenkins_job + "/", autoescape=True)


async def summary(db: AsyncSession, project: Project) -> dict:
    total, last = (
        await db.execute(select(func.count(), func.max(Incident.created_at)).where(incidents_of(project)))
    ).one()
    open_count = (
        await db.execute(select(func.count()).where(incidents_of(project), Incident.status == "OPEN"))
    ).scalar_one()
    pending = (
        await db.execute(
            select(func.count(func.distinct(RemediationProposal.incident_id)))
            .join(Incident, Incident.id == RemediationProposal.incident_id)
            .where(incidents_of(project), RemediationProposal.status == ProposalStatus.AWAITING_APPROVAL.value)
        )
    ).scalar_one()
    return {
        **project.to_dict(),
        "incident_count": total,
        "open_incidents": open_count,
        "pending_reviews": pending,
        "last_incident_at": last.isoformat() if last else None,
    }


@router.get("/projects")
async def list_projects(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    return [await summary(db, project) for project in await visible_projects(db, user)]


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectBody, user: User = Depends(require_role("reviewer")), db: AsyncSession = Depends(get_db)
) -> dict:
    clash = (
        await db.execute(select(Project).where(Project.archived.is_(False), Project.jenkins_job == body.jenkins_job))
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"The Jenkins job '{body.jenkins_job}' is already registered")
    project = Project(
        name=body.name,
        description=body.description,
        jenkins_job=body.jenkins_job,
        github_repo=body.github_repo,
        prometheus_job=body.prometheus_job,
        created_by=user.id,
    )
    db.add(project)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="A project with this name already exists") from error
    db.add(ProjectMember(project_id=project.id, user_id=user.id))
    audit(db, user.email, AuditEventType.PROJECT_CREATED.value, f"created project {project.name}", {"project": project.name, "jenkins_job": project.jenkins_job, "github_repo": project.github_repo})
    await db.commit()
    return await summary(db, project)


@router.get("/projects/{project_id}")
async def get_project(project_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    return await summary(db, await project_for(db, user, project_id))


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: int, body: ProjectPatch, user: User = Depends(require_role("reviewer")), db: AsyncSession = Depends(get_db)
) -> dict:
    project = await project_for(db, user, project_id)
    if body.name is not None:
        project.name = body.name.strip()
    if body.description is not None:
        project.description = body.description.strip()
    if body.prometheus_job is not None:
        candidate = ProjectBody(name=project.name, jenkins_job=project.jenkins_job, github_repo=project.github_repo, prometheus_job=body.prometheus_job)
        project.prometheus_job = candidate.prometheus_job
    if body.archived is not None:
        if user.role != "admin" and project.created_by != user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Only the creator or an admin can archive a project")
        project.archived = body.archived
    audit(db, user.email, AuditEventType.PROJECT_CHANGED.value, f"updated project {project.name}", {"project": project.name, "archived": bool(project.archived)})
    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="A project with this name already exists") from error
    return await summary(db, project)


@router.get("/projects/{project_id}/members")
async def get_members(project_id: int, user: User = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)) -> list[dict]:
    await project_for(db, user, project_id)
    rows = (
        await db.execute(
            select(User).join(ProjectMember, ProjectMember.user_id == User.id).where(ProjectMember.project_id == project_id).order_by(User.email)
        )
    ).scalars()
    return [u.to_dict() for u in rows]


@router.put("/projects/{project_id}/members")
async def set_members(
    project_id: int, body: MembersBody, user: User = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    project = await project_for(db, user, project_id)
    wanted = set(body.user_ids)
    known = set((await db.execute(select(User.id).where(User.id.in_(wanted)))).scalars()) if wanted else set()
    if wanted - known:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown user in the member list")
    current = set((await db.execute(select(ProjectMember.user_id).where(ProjectMember.project_id == project_id))).scalars())
    for user_id in wanted - current:
        db.add(ProjectMember(project_id=project_id, user_id=user_id))
    for row in (await db.execute(select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id.in_(current - wanted)))).scalars():
        await db.delete(row)
    audit(db, user.email, AuditEventType.PROJECT_CHANGED.value, f"changed members of {project.name}", {"project": project.name, "members": sorted(wanted)})
    await db.commit()
    return await get_members(project_id, user, db)
