"""darkflow playbook query indexes

Revision ID: 20260516_0010
Revises: 20260514_0009
Create Date: 2026-05-16 00:10:00
"""
from __future__ import annotations

from alembic import op

revision = "20260516_0010"
down_revision = "20260514_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_feature_events_indicator_ts_id",
        "feature_events",
        ["indicator", "event_ts", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_feature_events_indicator_ts_id", table_name="feature_events")
