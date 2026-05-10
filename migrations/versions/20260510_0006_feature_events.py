"""feature event research layer

Revision ID: 20260510_0006
Revises: 20260509_0005
Create Date: 2026-05-10 00:06:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260510_0006"
down_revision = "20260509_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feature_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("asset_tier", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("indicator", sa.String(length=64), nullable=False),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("feature_name", sa.String(length=120), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_price", sa.Float(), nullable=True),
        sa.Column("strength", sa.Float(), nullable=False),
        sa.Column("subtype", sa.String(length=80), nullable=False),
        sa.Column("source_payload_key", sa.String(length=120), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_key", name="uq_feature_events_event_key"),
    )
    op.create_index("ix_feature_events_snapshot_id", "feature_events", ["snapshot_id"])
    op.create_index("ix_feature_events_symbol", "feature_events", ["symbol"])
    op.create_index("ix_feature_events_asset_tier", "feature_events", ["asset_tier"])
    op.create_index("ix_feature_events_timeframe", "feature_events", ["timeframe"])
    op.create_index("ix_feature_events_interval", "feature_events", ["interval"])
    op.create_index("ix_feature_events_indicator", "feature_events", ["indicator"])
    op.create_index("ix_feature_events_event_key", "feature_events", ["event_key"])
    op.create_index("ix_feature_events_feature_name", "feature_events", ["feature_name"])
    op.create_index("ix_feature_events_direction", "feature_events", ["direction"])
    op.create_index("ix_feature_events_event_ts", "feature_events", ["event_ts"])
    op.create_index("ix_feature_events_subtype", "feature_events", ["subtype"])
    op.create_index("ix_feature_events_source_payload_key", "feature_events", ["source_payload_key"])
    op.create_index("ix_feature_events_created_at", "feature_events", ["created_at"])
    op.create_index(
        "ix_feature_events_indicator_feature_ts",
        "feature_events",
        ["indicator", "feature_name", "event_ts"],
    )
    op.create_index(
        "ix_feature_events_symbol_timeframe_ts",
        "feature_events",
        ["symbol", "timeframe", "event_ts"],
    )

    op.create_table(
        "feature_labels",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("feature_event_id", sa.String(length=36), nullable=False),
        sa.Column("horizon", sa.String(length=16), nullable=False),
        sa.Column("return_pct", sa.Float(), nullable=True),
        sa.Column("mfe", sa.Float(), nullable=True),
        sa.Column("mae", sa.Float(), nullable=True),
        sa.Column("future_price", sa.Float(), nullable=True),
        sa.Column("future_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("feature_event_id", "horizon", name="uq_feature_labels_event_horizon"),
    )
    op.create_index("ix_feature_labels_feature_event_id", "feature_labels", ["feature_event_id"])
    op.create_index("ix_feature_labels_horizon", "feature_labels", ["horizon"])
    op.create_index("ix_feature_labels_status", "feature_labels", ["status"])
    op.create_index("ix_feature_labels_created_at", "feature_labels", ["created_at"])
    op.create_index("ix_feature_labels_updated_at", "feature_labels", ["updated_at"])
    op.create_index("ix_feature_labels_horizon_status", "feature_labels", ["horizon", "status"])


def downgrade() -> None:
    op.drop_table("feature_labels")
    op.drop_table("feature_events")
