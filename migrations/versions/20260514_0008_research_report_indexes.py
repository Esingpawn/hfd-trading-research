"""research report query indexes

Revision ID: 20260514_0008
Revises: 20260511_0007
Create Date: 2026-05-14 00:08:00
"""
from __future__ import annotations

from alembic import op

revision = "20260514_0008"
down_revision = "20260511_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_feature_labels_horizon_status_event",
        "feature_labels",
        ["horizon", "status", "feature_event_id"],
    )
    op.create_index(
        "ix_feature_events_event_ts_id",
        "feature_events",
        ["event_ts", "id"],
    )
    op.create_index(
        "ix_experiment_runs_name_status_created",
        "experiment_runs",
        ["name", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_experiment_runs_name_status_created", table_name="experiment_runs")
    op.drop_index("ix_feature_events_event_ts_id", table_name="feature_events")
    op.drop_index("ix_feature_labels_horizon_status_event", table_name="feature_labels")
