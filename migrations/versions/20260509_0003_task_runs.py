"""task runs

Revision ID: 20260509_0003
Revises: 20260509_0002
Create Date: 2026-05-09 00:03:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260509_0003"
down_revision = "20260509_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_name", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_task_runs_task_name", "task_runs", ["task_name"])
    op.create_index("ix_task_runs_status", "task_runs", ["status"])
    op.create_index("ix_task_runs_queued_at", "task_runs", ["queued_at"])


def downgrade() -> None:
    op.drop_table("task_runs")
