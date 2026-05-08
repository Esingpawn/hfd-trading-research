"""experiment governance

Revision ID: 20260509_0004
Revises: 20260509_0003
Create Date: 2026-05-09 00:04:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260509_0004"
down_revision = "20260509_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "experiment_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_experiment_runs_name", "experiment_runs", ["name"])
    op.create_index("ix_experiment_runs_status", "experiment_runs", ["status"])
    op.create_index("ix_experiment_runs_created_at", "experiment_runs", ["created_at"])
    op.create_table(
        "weight_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("weights", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_weight_versions_name", "weight_versions", ["name"])
    op.create_index("ix_weight_versions_status", "weight_versions", ["status"])
    op.create_index("ix_weight_versions_created_at", "weight_versions", ["created_at"])


def downgrade() -> None:
    op.drop_table("weight_versions")
    op.drop_table("experiment_runs")
