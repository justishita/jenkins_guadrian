"""The 0003 migration creates exactly the tables the UI models use, and rolls back cleanly."""

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect

from backend.db.ui_models import Project, ProjectMember, User

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def database_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{(tmp_path / 'ui.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def test_the_migration_creates_the_ui_tables_and_their_columns(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")
    inspector = inspect(create_engine(database_url))
    assert {"users", "projects", "project_members"} <= set(inspector.get_table_names())
    for model in (User, Project, ProjectMember):
        migrated = {c["name"] for c in inspector.get_columns(model.__tablename__)}
        assert migrated == {c.name for c in model.__table__.columns}, model.__tablename__


def test_email_is_unique_in_the_migrated_schema(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")
    indexes = {i["name"]: i for i in inspect(create_engine(database_url)).get_indexes("users")}
    assert indexes["ix_users_email"]["unique"]


def test_the_migration_is_reversible(database_url: str) -> None:
    config = alembic_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0002_approval_contract")
    assert not {"users", "projects", "project_members"} & set(inspect(create_engine(database_url)).get_table_names())
