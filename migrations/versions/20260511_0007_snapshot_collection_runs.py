"""link signal snapshots to collection runs

Revision ID: 20260511_0007
Revises: 20260510_0006
Create Date: 2026-05-11 00:07:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260511_0007"
down_revision = "20260510_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signal_snapshots", sa.Column("collection_run_id", sa.String(length=36), nullable=True))
    op.create_index("ix_signal_snapshots_collection_run_id", "signal_snapshots", ["collection_run_id"])


def downgrade() -> None:
    op.drop_index("ix_signal_snapshots_collection_run_id", table_name="signal_snapshots")
    op.drop_column("signal_snapshots", "collection_run_id")
