"""Approvals table to the shared approval contract.

The contract agreed between P1 (approval UI and gateway), P2 (approval dashboards) and
P3 (this table) is ``approvals: incident_id, attempt, decision, reviewer, comment,
decided_at``. 0001 created the table without ``attempt`` and called the reviewer's
note ``reason``; this adds the one, renames the other, and allows only one decision
per incident attempt.

Batch mode so the same migration runs on SQLite in the tests; on PostgreSQL it is
plain ALTER TABLE.

Revision ID: 0002_approval_contract
Revises: 0001_audit_layer
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002_approval_contract"
down_revision: str | None = "0001_audit_layer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.add_column(sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"))
        batch.alter_column("reason", new_column_name="comment", existing_type=sa.Text(), existing_nullable=False)
        batch.create_unique_constraint("uq_approvals_incident_attempt", ["incident_id", "attempt"])


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.drop_constraint("uq_approvals_incident_attempt", type_="unique")
        batch.alter_column("comment", new_column_name="reason", existing_type=sa.Text(), existing_nullable=False)
        batch.drop_column("attempt")
