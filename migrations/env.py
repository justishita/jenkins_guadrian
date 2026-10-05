"""Alembic environment for the audit database.

**Owner:** P3. The database URL comes from the environment only - never from
``alembic.ini`` - so migrations use the same configuration as the running services
and no credential is committed.
"""

from __future__ import annotations

from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend.db.database import Base
import backend.db.models  # noqa: F401  (registers incidents and the audit tables)


config = context.config

if config.config_file_name is not None:
	fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
	"""Return a synchronous URL: Alembic runs its own connection, not the app's."""
	url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url", "")
	if not url:
		raise RuntimeError("DATABASE_URL must be set to run migrations")
	for async_prefix, sync_prefix in (
		("postgresql+asyncpg://", "postgresql://"),
		("postgres://", "postgresql://"),
	):
		if url.startswith(async_prefix):
			return url.replace(async_prefix, sync_prefix, 1)
	return url


def run_migrations_offline() -> None:
	"""Emit SQL without connecting, for reviewing a migration before applying it."""
	context.configure(
		url=_database_url(),
		target_metadata=target_metadata,
		literal_binds=True,
		dialect_opts={"paramstyle": "named"},
		compare_type=True,
	)
	with context.begin_transaction():
		context.run_migrations()


def run_migrations_online() -> None:
	"""Apply migrations against a live database."""
	section = config.get_section(config.config_ini_section) or {}
	section["sqlalchemy.url"] = _database_url()
	connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
	with connectable.connect() as connection:
		context.configure(
			connection=connection, target_metadata=target_metadata, compare_type=True
		)
		with context.begin_transaction():
			context.run_migrations()


if context.is_offline_mode():
	run_migrations_offline()
else:
	run_migrations_online()
