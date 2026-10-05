"""Tests for the audit-layer schema and its migration.

**Owner:** P3. These run against SQLite rather than PostgreSQL so they stay in the
normal ``pytest`` run; what they verify is dialect-independent - that the migration
applies to both a fresh and an already-provisioned database, that it rolls back, and
that the tables it creates match the models the application queries through.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect

from backend.db.audit_models import (
	AgentRunStatus,
	ApprovalDecision,
	AuditEventType,
	ProposalStatus,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

AUDIT_TABLES = {
	"audit_events",
	"agent_runs",
	"remediation_proposals",
	"policy_decisions",
	"approvals",
}


def alembic_config(database_url: str) -> Config:
	config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
	config.set_main_option("script_location", str(REPOSITORY_ROOT / "migrations"))
	config.set_main_option("sqlalchemy.url", database_url)
	return config


@pytest.fixture
def database_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
	url = f"sqlite:///{(tmp_path / 'audit.db').as_posix()}"
	# migrations/env.py reads DATABASE_URL, and importing it must not pick up a
	# developer's real database from the ambient environment.
	monkeypatch.setenv("DATABASE_URL", url)
	return url


def test_migration_creates_the_audit_tables_on_a_fresh_database(database_url: str) -> None:
	command.upgrade(alembic_config(database_url), "head")

	tables = set(inspect(create_engine(database_url)).get_table_names())
	assert AUDIT_TABLES <= tables
	assert "incidents" in tables


def test_migration_adds_the_duplicate_incident_link(database_url: str) -> None:
	command.upgrade(alembic_config(database_url), "head")

	columns = {
		column["name"]
		for column in inspect(create_engine(database_url)).get_columns("incidents")
	}
	assert "related_incident_id" in columns


def test_migration_is_reversible(database_url: str) -> None:
	config = alembic_config(database_url)
	command.upgrade(config, "head")
	command.downgrade(config, "base")

	inspector = inspect(create_engine(database_url))
	assert AUDIT_TABLES & set(inspector.get_table_names()) == set()
	columns = {column["name"] for column in inspector.get_columns("incidents")}
	assert "related_incident_id" not in columns


def test_migration_applies_to_a_database_the_backend_already_provisioned(
	database_url: str,
) -> None:
	"""`init_db()` creates tables with `create_all`; the migration must still apply."""
	from backend.db.database import Base
	import backend.db.models  # noqa: F401  (registers the tables)

	engine = create_engine(database_url)
	# Only the pre-existing operational table, as an older deployment would have it.
	Base.metadata.tables["incidents"].create(engine)

	command.upgrade(alembic_config(database_url), "head")

	assert AUDIT_TABLES <= set(inspect(engine).get_table_names())


def test_models_and_migration_agree_on_the_audit_columns(database_url: str) -> None:
	from backend.db.database import Base
	import backend.db.models  # noqa: F401

	command.upgrade(alembic_config(database_url), "head")
	inspector = inspect(create_engine(database_url))

	for table_name in sorted(AUDIT_TABLES):
		migrated = {column["name"] for column in inspector.get_columns(table_name)}
		declared = set(Base.metadata.tables[table_name].columns.keys())
		assert migrated == declared, f"{table_name} drifted between the model and the migration"


@pytest.mark.parametrize(
	"enum_type",
	[AuditEventType, AgentRunStatus, ProposalStatus, ApprovalDecision],
)
def test_audit_vocabularies_are_plain_strings(enum_type: type) -> None:
	"""Rows store the value, so each member must serialise to its own string."""
	for member in enum_type:  # type: ignore[attr-defined]
		assert isinstance(member.value, str)
		assert member.value == member.value.lower()


def test_agent_run_status_covers_the_evidence_statuses() -> None:
	"""An agent run must be recordable for every status the evidence contract allows."""
	from common.models import Evidence

	evidence_statuses = set(Evidence.model_fields["status"].annotation.__args__)  # type: ignore[union-attr]
	assert evidence_statuses <= {member.value for member in AgentRunStatus}
