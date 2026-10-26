"""Fixtures for the UI API tests: a real SQLite database, temp report/blackboard directories, a signed-in client."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.config import settings
from backend.db.audit_models import AuditEvent, PolicyDecision, ProposalStatus, RemediationProposal
from backend.db.database import Base, get_db
from backend.db.models import Incident
from backend.db.ui_models import Project, ProjectMember, User
from backend.main import app
from backend.ui.security import hash_password, limiter
from common.blackboard import FileBlackboard

PASSWORD = "correct horse battery"


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = (tmp_path / "ui.db").as_posix()
    sync_engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(sync_engine)
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False, class_=AsyncSession)

    async def override_db():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(settings, "UI_SESSION_SECRET", "s" * 48)
    monkeypatch.setattr(settings, "REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "UI_LOGIN_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(settings, "UI_BCRYPT_ROUNDS", 4)  # real cost in production; fast here
    limiter._failures.clear()
    yield sync_engine
    app.dependency_overrides.pop(get_db, None)
    sync_engine.dispose()


@pytest.fixture
def blackboard(tmp_path: Path):
    board = FileBlackboard(tmp_path / "blackboard")
    with patch("backend.routes.approvals.get_blackboard", return_value=board):
        yield board


@pytest.fixture
def published():
    with patch("backend.routes.approvals.publisher.publish_approval_decided", new_callable=AsyncMock, return_value=True) as mock:
        yield mock


@pytest.fixture
def app_client(database, blackboard, published) -> TestClient:
    return TestClient(app)


class World:
    """Helpers that write straight to the database and disk, bypassing the API under test."""

    def __init__(self, engine, tmp_path: Path) -> None:
        self.engine, self.tmp_path = engine, tmp_path

    def user(self, email: str, role: str = "viewer", *, active: bool = True, password: str = PASSWORD) -> int:
        with Session(self.engine) as session:
            user = User(email=email, name=email.split("@")[0], password_hash=hash_password(password), role=role, active=active)
            session.add(user)
            session.commit()
            return user.id

    def project(self, name: str, job: str, members: list[int] = (), *, repo: str = "acme/shop", archived: bool = False) -> int:
        with Session(self.engine) as session:
            project = Project(name=name, jenkins_job=job, github_repo=repo, archived=archived)
            session.add(project)
            session.flush()
            for user_id in members:
                session.add(ProjectMember(project_id=project.id, user_id=user_id))
            session.commit()
            return project.id

    def incident(self, job: str = "target-app/main", *, status: str = "OPEN", age_hours: float = 1.0, build: int = 7) -> str:
        incident_id = str(uuid4())
        with Session(self.engine) as session:
            session.add(
                Incident(
                    id=incident_id, job_name=job, build_number=build, build_url=f"http://jenkins/job/{job}/{build}/", branch="main",
                    git_commit="abc1234", failed_stage="Test", status=status, event_key=f"e-{incident_id}",
                    created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
                )
            )
            session.commit()
        return incident_id

    def proposal(self, incident_id: str, *, status: str = ProposalStatus.AWAITING_APPROVAL.value, allowed: bool = True, attempt: int = 0) -> int:
        with Session(self.engine) as session:
            proposal = RemediationProposal(
                incident_id=incident_id, attempt=attempt, status=status, failure_type="dependency_regression",
                rationale="Revert the pin.", diff="-x==2\n+x==1\n", changed_files=["target_app/requirements.txt"], confidence=0.85,
            )
            session.add(proposal)
            session.flush()
            session.add(PolicyDecision(proposal_id=proposal.id, incident_id=incident_id, policy_package="jenkinsguardians/remediation", allowed=allowed, violations=[] if allowed else ["x"], warnings=[]))
            session.commit()
            return proposal.id

    def audit(self, incident_id: str, event_type: str, *, actor: str = "coordinator", payload: dict | None = None, duration_ms: float | None = None, ok: bool | None = None, age_hours: float = 0.5) -> None:
        with Session(self.engine) as session:
            session.add(
                AuditEvent(
                    incident_id=incident_id, actor=actor, event_type=event_type, summary=event_type, payload=payload or {},
                    duration_ms=duration_ms, ok=ok, created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
                )
            )
            session.commit()

    def report(self, incident_id: str, *, verdict: str = "dependency_regression", confidence: float = 0.85, status: str = "resolved_cause",
               missing: list[str] = (), outcomes: dict | None = None, attempt: int = 0, revision: int = 1, findings: list[dict] | None = None) -> None:
        directory = self.tmp_path / "reports" / incident_id
        directory.mkdir(parents=True, exist_ok=True)
        report = {
            "incident_id": incident_id, "created_at": "2026-10-26T09:00:00Z", "status": status, "failure_type": verdict,
            "confidence": confidence, "summary": f"{verdict} at {confidence:.2f} confidence.",
            "findings": findings if findings is not None else [
                {"agent": "jenkins_agent", "status": "completed", "failure_type": verdict, "confidence": confidence, "summary": "j", "abstained": False},
                {"agent": "code_agent", "status": "completed", "failure_type": verdict, "confidence": 0.7, "summary": "c", "abstained": False},
            ],
            "agreeing_agents": ["jenkins_agent", "code_agent"], "abstaining_agents": [], "missing_agents": list(missing),
            "agent_outcomes": outcomes if outcomes is not None else {"jenkins_agent": "completed", "code_agent": "completed"},
            "conflicts": [], "hypotheses": [], "recommended_next_steps": ["Pin the dependency."],
        }
        envelope = {
            "report_revision_id": f"{incident_id}:{attempt}:abc{revision}", "incident_id": incident_id, "attempt": attempt,
            "revision": revision, "fingerprint": f"abc{revision}", "reason": "initial", "created_at": "2026-10-26T09:00:00+00:00", "report": report,
        }
        (directory / f"a{attempt}-r{revision}.json").write_text(json.dumps(envelope), encoding="utf-8")


@pytest.fixture
def world(database, tmp_path) -> World:
    return World(database, tmp_path)


class Session_:
    """A TestClient plus the CSRF token of its signed-in session."""

    def __init__(self, client: TestClient, csrf: str) -> None:
        self.client, self.csrf = client, csrf

    def get(self, url, **kw):
        return self.client.get(url, **kw)

    def post(self, url, **kw):
        return self.client.post(url, headers={"X-CSRF-Token": self.csrf, **kw.pop("headers", {})}, **kw)

    def patch(self, url, **kw):
        return self.client.patch(url, headers={"X-CSRF-Token": self.csrf, **kw.pop("headers", {})}, **kw)

    def put(self, url, **kw):
        return self.client.put(url, headers={"X-CSRF-Token": self.csrf, **kw.pop("headers", {})}, **kw)


@pytest.fixture
def sign_in(app_client):
    """``sign_in(email)`` -> a client signed in as that user (each call gets its own cookie jar)."""

    def make(email: str, password: str = PASSWORD) -> Session_:
        client = TestClient(app)
        response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        return Session_(client, response.json()["csrf_token"])

    return make
