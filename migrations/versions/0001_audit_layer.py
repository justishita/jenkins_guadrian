"""Audit layer: agent runs, audit events, proposals, policy decisions, approvals.

Also adds ``incidents.related_incident_id`` so a repeated failure (TC-19) is linked to
the incident it recurs from instead of being investigated from scratch.

Baseline migration: it creates ``incidents`` only when the table is absent, so it
applies cleanly both to a fresh database and to an existing one that the backend's
``create_all`` already provisioned.

Revision ID: 0001_audit_layer
Revises:
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_audit_layer"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


JSON_PAYLOAD = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {item["name"] for item in sa.inspect(bind).get_columns(table)}


def _supports_alter_constraints() -> bool:
    """SQLite cannot ALTER TABLE ADD CONSTRAINT.

    Deployments run PostgreSQL; SQLite is only used to exercise the migration in
    tests, where a self-referencing foreign key adds nothing worth the batch-mode
    table rebuild it would cost.
    """
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    if not _has_table("incidents"):
        op.create_table(
            "incidents",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("job_name", sa.String(255), nullable=False),
            sa.Column("build_number", sa.Integer(), nullable=False),
            sa.Column("build_url", sa.Text(), nullable=False),
            sa.Column("branch", sa.String(255), nullable=False),
            sa.Column("git_commit", sa.String(64), nullable=False),
            sa.Column("failed_stage", sa.String(255), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="OPEN"),
            sa.Column("event_key", sa.String(64), nullable=False, unique=True),
            sa.Column("remediation_attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_incidents_id", "incidents", ["id"])
        op.create_index("ix_incidents_event_key", "incidents", ["event_key"], unique=True)

    if not _has_column("incidents", "related_incident_id"):
        op.add_column(
            "incidents",
            sa.Column("related_incident_id", sa.String(36), nullable=True),
        )
        op.create_index(
            "ix_incidents_related_incident_id", "incidents", ["related_incident_id"]
        )
        if _supports_alter_constraints():
            op.create_foreign_key(
                "fk_incidents_related_incident_id",
                "incidents",
                "incidents",
                ["related_incident_id"],
                ["id"],
            )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.String(36), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", JSON_PAYLOAD, nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_events_incident_id", "audit_events", ["incident_id"])
    op.create_index(
        "ix_audit_events_incident_created", "audit_events", ["incident_id", "created_at"]
    )
    op.create_index("ix_audit_events_actor_type", "audit_events", ["actor", "event_type"])

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.String(36), nullable=False),
        sa.Column("agent", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("failure_type", sa.String(64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("evidence_version", sa.Integer(), nullable=True),
        sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
    )
    op.create_index("ix_agent_runs_incident_id", "agent_runs", ["incident_id"])
    op.create_index("ix_agent_runs_incident_agent", "agent_runs", ["incident_id", "agent"])

    op.create_table(
        "remediation_proposals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.String(36), nullable=False),
        sa.Column("proposed_by", sa.String(64), nullable=False, server_default="code_agent"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("failure_type", sa.String(64), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("diff", sa.Text(), nullable=False, server_default=""),
        sa.Column("changed_files", JSON_PAYLOAD, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("pull_request_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_remediation_proposals_incident_id", "remediation_proposals", ["incident_id"]
    )

    op.create_table(
        "policy_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "proposal_id",
            sa.Integer(),
            sa.ForeignKey("remediation_proposals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("incident_id", sa.String(36), nullable=False),
        sa.Column("policy_package", sa.String(128), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("violations", JSON_PAYLOAD, nullable=False),
        sa.Column("warnings", JSON_PAYLOAD, nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_policy_decisions_proposal_id", "policy_decisions", ["proposal_id"])
    op.create_index("ix_policy_decisions_incident_id", "policy_decisions", ["incident_id"])

    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "proposal_id",
            sa.Integer(),
            sa.ForeignKey("remediation_proposals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("incident_id", sa.String(36), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reviewer", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_approvals_proposal_id", "approvals", ["proposal_id"])
    op.create_index("ix_approvals_incident_id", "approvals", ["incident_id"])


def downgrade() -> None:
    op.drop_table("approvals")
    op.drop_table("policy_decisions")
    op.drop_table("remediation_proposals")
    op.drop_table("agent_runs")
    op.drop_table("audit_events")

    if _has_column("incidents", "related_incident_id"):
        if _supports_alter_constraints():
            op.drop_constraint("fk_incidents_related_incident_id", "incidents", type_="foreignkey")
        op.drop_index("ix_incidents_related_incident_id", table_name="incidents")
        op.drop_column("incidents", "related_incident_id")
