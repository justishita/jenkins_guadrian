"""Sign-in, sessions, CSRF, roles and user management."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from backend.config import settings
from backend.db.audit_models import AuditEvent
from backend.db.ui_models import User
from backend.main import app
from backend.ui.security import issue_session, password_problem, read_session
from backend.ui.seed import seed_admin
from tests.ui.conftest import PASSWORD


def login(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def test_signing_in_sets_an_httponly_samesite_cookie_and_returns_a_csrf_token(app_client, world):
    world.user("ana@acme.io", "viewer")
    response = login(app_client, "ana@acme.io")
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == "ana@acme.io" and body["csrf_token"]
    assert "password" not in json_text(body)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie


def json_text(value) -> str:
    return str(value).lower()


def test_the_email_is_case_insensitive(app_client, world):
    world.user("ana@acme.io")
    assert login(app_client, "  ANA@Acme.IO ").status_code == 200


def test_me_returns_the_signed_in_user_and_401_without_a_session(app_client, world):
    world.user("ana@acme.io", "reviewer")
    assert app_client.get("/api/v1/auth/me").status_code == 401
    login(app_client, "ana@acme.io")
    assert app_client.get("/api/v1/auth/me").json()["user"]["role"] == "reviewer"


def test_a_wrong_password_and_an_unknown_email_give_the_same_answer(app_client, world):
    world.user("ana@acme.io")
    wrong = login(app_client, "ana@acme.io", "not the password")
    unknown = login(app_client, "nobody@acme.io")
    assert (wrong.status_code, wrong.json()) == (unknown.status_code, unknown.json()) == (401, {"detail": "Invalid email or password"})


def test_a_deactivated_user_cannot_sign_in(app_client, world):
    world.user("gone@acme.io", active=False)
    assert login(app_client, "gone@acme.io").status_code == 401


def test_repeated_failures_are_rate_limited_even_with_the_right_password_afterwards(app_client, world):
    world.user("ana@acme.io")
    for _ in range(5):
        assert login(app_client, "ana@acme.io", "wrong wrong wrong").status_code == 401
    assert login(app_client, "ana@acme.io").status_code == 429  # blocked, not just slowed


def test_a_successful_sign_in_clears_the_failure_count(app_client, world):
    world.user("ana@acme.io")
    for _ in range(4):
        login(app_client, "ana@acme.io", "wrong wrong wrong")
    assert login(app_client, "ana@acme.io").status_code == 200
    for _ in range(4):
        assert login(app_client, "ana@acme.io", "wrong wrong wrong").status_code == 401


def test_sign_in_is_refused_when_no_session_secret_is_configured(app_client, world, monkeypatch):
    world.user("ana@acme.io")
    monkeypatch.setattr(settings, "UI_SESSION_SECRET", "too short")
    assert login(app_client, "ana@acme.io").status_code == 503


def test_state_changing_requests_need_the_csrf_header(app_client, world):
    world.user("ana@acme.io")
    login(app_client, "ana@acme.io")
    assert app_client.post("/api/v1/auth/logout").status_code == 403  # cookie alone is not enough
    assert app_client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403


def test_logout_ends_the_session_in_the_browser(sign_in, world):
    world.user("ana@acme.io")
    ana = sign_in("ana@acme.io")
    assert ana.post("/api/v1/auth/logout").status_code == 204
    assert ana.get("/api/v1/auth/me").status_code == 401


def test_a_tampered_or_expired_session_is_rejected(app_client, world):
    user_id = world.user("ana@acme.io")
    token, _ = issue_session(user_id, 1)
    assert read_session(token) is not None
    assert read_session(token[:-3] + "abc") is None
    old, _ = issue_session(user_id, 1, now=datetime.now(timezone.utc) - timedelta(hours=settings.UI_SESSION_HOURS + 1))
    assert read_session(old) is None
    app_client.cookies.set(settings.UI_COOKIE_NAME, old)
    assert app_client.get("/api/v1/auth/me").status_code == 401


def test_a_session_for_a_user_who_was_deactivated_stops_working(sign_in, world, database):
    user_id = world.user("ana@acme.io")
    ana = sign_in("ana@acme.io")
    with Session(database) as session:
        session.get(User, user_id).active = False
        session.commit()
    assert ana.get("/api/v1/auth/me").status_code == 401


def test_changing_the_password_ends_other_sessions_and_keeps_this_one(sign_in, world):
    world.user("ana@acme.io")
    first, second = sign_in("ana@acme.io"), sign_in("ana@acme.io")
    response = first.post("/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": "a brand new passphrase"})
    assert response.status_code == 200
    assert first.get("/api/v1/auth/me").status_code == 200
    assert second.get("/api/v1/auth/me").status_code == 401
    assert login(TestClient(app), "ana@acme.io").status_code == 401  # the old password no longer works


def test_the_current_password_must_be_right_to_change_it(sign_in, world):
    world.user("ana@acme.io")
    ana = sign_in("ana@acme.io")
    assert ana.post("/api/v1/auth/password", json={"current_password": "nope nope nope", "new_password": "a brand new passphrase"}).status_code == 403


@pytest.mark.parametrize(("password", "fragment"), [("short", "at least"), ("a" * 80, "at most"), ("ana@acme.io", "same as the email")])
def test_password_rules(password, fragment):
    assert fragment in (password_problem(password, "ana@acme.io") or "")


def test_sign_ins_and_failures_are_audited_without_secrets(app_client, world, database):
    world.user("ana@acme.io")
    login(app_client, "ana@acme.io", "wrong wrong wrong")
    login(app_client, "ana@acme.io")
    with Session(database) as session:
        events = session.query(AuditEvent).filter(AuditEvent.event_type.in_(["user_login", "user_login_failed"])).all()
    assert sorted(e.event_type for e in events) == ["user_login", "user_login_failed"]
    assert PASSWORD not in "".join(str(e.payload) + e.summary for e in events)


# ---- roles and user management ------------------------------------------------------------------


def test_only_admins_manage_users(sign_in, world):
    world.user("admin@acme.io", "admin")
    world.user("rev@acme.io", "reviewer")
    assert sign_in("rev@acme.io").get("/api/v1/users").status_code == 403
    assert sign_in("admin@acme.io").get("/api/v1/users").status_code == 200


def test_an_admin_creates_a_user_who_can_then_sign_in(sign_in, app_client, world):
    world.user("admin@acme.io", "admin")
    admin = sign_in("admin@acme.io")
    created = admin.post("/api/v1/users", json={"email": "New@Acme.io", "password": "a good long passphrase", "role": "reviewer"})
    assert created.status_code == 201 and created.json()["email"] == "new@acme.io" and "password_hash" not in created.json()
    assert login(TestClient(app), "new@acme.io", "a good long passphrase").status_code == 200


def test_user_creation_validates_email_password_and_duplicates(sign_in, world):
    world.user("admin@acme.io", "admin")
    admin = sign_in("admin@acme.io")
    assert admin.post("/api/v1/users", json={"email": "not-an-email", "password": "a good long passphrase"}).status_code == 422
    assert admin.post("/api/v1/users", json={"email": "x@acme.io", "password": "short"}).status_code == 422
    assert admin.post("/api/v1/users", json={"email": "admin@acme.io", "password": "a good long passphrase"}).status_code == 409
    assert admin.post("/api/v1/users", json={"email": "x@acme.io", "password": "a good long passphrase", "role": "root"}).status_code == 422


def test_the_last_active_admin_cannot_be_demoted_or_deactivated(sign_in, world):
    admin_id = world.user("admin@acme.io", "admin")
    admin = sign_in("admin@acme.io")
    assert admin.patch(f"/api/v1/users/{admin_id}", json={"role": "viewer"}).status_code == 409
    assert admin.patch(f"/api/v1/users/{admin_id}", json={"active": False}).status_code == 409
    second = world.user("two@acme.io", "admin")
    assert admin.patch(f"/api/v1/users/{second}", json={"active": False}).status_code == 200


def test_an_admin_reset_ends_that_users_sessions(sign_in, world):
    world.user("admin@acme.io", "admin")
    ana_id = world.user("ana@acme.io")
    admin, ana = sign_in("admin@acme.io"), sign_in("ana@acme.io")
    assert admin.patch(f"/api/v1/users/{ana_id}", json={"password": "a replacement passphrase"}).status_code == 200
    assert ana.get("/api/v1/auth/me").status_code == 401


# ---- seeding ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_admin_is_seeded_once_and_never_overwritten(database, monkeypatch):
    from backend.db import database as db_module

    path = database.url.render_as_string(hide_password=False).replace("sqlite:///", "")
    engine = db_module.create_async_engine(f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(db_module, "async_session", db_module.async_sessionmaker(engine, expire_on_commit=False, class_=db_module.AsyncSession))
    monkeypatch.setattr(settings, "UI_ADMIN_EMAIL", "Boss@Acme.io")
    monkeypatch.setattr(settings, "UI_ADMIN_PASSWORD", "a long seeded passphrase")
    assert await seed_admin() is True
    assert await seed_admin() is False  # users now exist
    with Session(database) as session:
        [user] = session.query(User).all()
    assert (user.email, user.role) == ("boss@acme.io", "admin")
    await engine.dispose()


@pytest.mark.asyncio
async def test_an_invalid_seed_is_ignored(database, monkeypatch):
    monkeypatch.setattr(settings, "UI_ADMIN_EMAIL", "boss@acme.io")
    monkeypatch.setattr(settings, "UI_ADMIN_PASSWORD", "short")
    assert await seed_admin() is False
