"""darkflow zone interaction research tables

Revision ID: 20260516_0011
Revises: 20260516_0010
Create Date: 2026-05-16 01:10:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260516_0011"
down_revision = "20260516_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "darkflow_zones",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("zone_key", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("source_event_id", sa.String(length=36), nullable=True),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("asset_tier", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("indicator", sa.String(length=64), nullable=False),
        sa.Column("family", sa.String(length=64), nullable=False),
        sa.Column("zone_type", sa.String(length=80), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("lower_price", sa.Float(), nullable=False),
        sa.Column("upper_price", sa.Float(), nullable=False),
        sa.Column("mid_price", sa.Float(), nullable=False),
        sa.Column("strength", sa.Float(), nullable=False),
        sa.Column("subtype", sa.String(length=80), nullable=False),
        sa.Column("origin_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("touches", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("zone_key", name="uq_darkflow_zones_zone_key"),
    )
    op.create_index("ix_darkflow_zones_zone_key", "darkflow_zones", ["zone_key"])
    op.create_index("ix_darkflow_zones_source_snapshot_id", "darkflow_zones", ["source_snapshot_id"])
    op.create_index("ix_darkflow_zones_source_event_id", "darkflow_zones", ["source_event_id"])
    op.create_index("ix_darkflow_zones_symbol", "darkflow_zones", ["symbol"])
    op.create_index("ix_darkflow_zones_asset_tier", "darkflow_zones", ["asset_tier"])
    op.create_index("ix_darkflow_zones_timeframe", "darkflow_zones", ["timeframe"])
    op.create_index("ix_darkflow_zones_interval", "darkflow_zones", ["interval"])
    op.create_index("ix_darkflow_zones_indicator", "darkflow_zones", ["indicator"])
    op.create_index("ix_darkflow_zones_family", "darkflow_zones", ["family"])
    op.create_index("ix_darkflow_zones_zone_type", "darkflow_zones", ["zone_type"])
    op.create_index("ix_darkflow_zones_direction", "darkflow_zones", ["direction"])
    op.create_index("ix_darkflow_zones_origin_ts", "darkflow_zones", ["origin_ts"])
    op.create_index("ix_darkflow_zones_detected_at", "darkflow_zones", ["detected_at"])
    op.create_index("ix_darkflow_zones_expires_at", "darkflow_zones", ["expires_at"])
    op.create_index("ix_darkflow_zones_status", "darkflow_zones", ["status"])
    op.create_index("ix_darkflow_zones_created_at", "darkflow_zones", ["created_at"])
    op.create_index(
        "ix_darkflow_zones_symbol_timeframe_detected",
        "darkflow_zones",
        ["symbol", "timeframe", "detected_at"],
    )
    op.create_index(
        "ix_darkflow_zones_indicator_detected",
        "darkflow_zones",
        ["indicator", "detected_at"],
    )
    op.create_index(
        "ix_darkflow_zones_status_detected",
        "darkflow_zones",
        ["status", "detected_at"],
    )

    op.create_table(
        "darkflow_interactions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("interaction_key", sa.String(length=64), nullable=False),
        sa.Column("zone_id", sa.String(length=36), nullable=True),
        sa.Column("zone_key", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("indicator", sa.String(length=64), nullable=False),
        sa.Column("playbook", sa.String(length=80), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("interaction_type", sa.String(length=64), nullable=False),
        sa.Column("event_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("target_price", sa.Float(), nullable=True),
        sa.Column("invalidation_price", sa.Float(), nullable=True),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("exit_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_reason", sa.String(length=64), nullable=True),
        sa.Column("pnl_pct", sa.Float(), nullable=True),
        sa.Column("r_multiple", sa.Float(), nullable=True),
        sa.Column("mfe", sa.Float(), nullable=False),
        sa.Column("mae", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("interaction_key", name="uq_darkflow_interactions_interaction_key"),
    )
    op.create_index("ix_darkflow_interactions_interaction_key", "darkflow_interactions", ["interaction_key"])
    op.create_index("ix_darkflow_interactions_zone_id", "darkflow_interactions", ["zone_id"])
    op.create_index("ix_darkflow_interactions_zone_key", "darkflow_interactions", ["zone_key"])
    op.create_index("ix_darkflow_interactions_source_snapshot_id", "darkflow_interactions", ["source_snapshot_id"])
    op.create_index("ix_darkflow_interactions_symbol", "darkflow_interactions", ["symbol"])
    op.create_index("ix_darkflow_interactions_timeframe", "darkflow_interactions", ["timeframe"])
    op.create_index("ix_darkflow_interactions_interval", "darkflow_interactions", ["interval"])
    op.create_index("ix_darkflow_interactions_indicator", "darkflow_interactions", ["indicator"])
    op.create_index("ix_darkflow_interactions_playbook", "darkflow_interactions", ["playbook"])
    op.create_index("ix_darkflow_interactions_direction", "darkflow_interactions", ["direction"])
    op.create_index("ix_darkflow_interactions_interaction_type", "darkflow_interactions", ["interaction_type"])
    op.create_index("ix_darkflow_interactions_event_ts", "darkflow_interactions", ["event_ts"])
    op.create_index("ix_darkflow_interactions_exit_reason", "darkflow_interactions", ["exit_reason"])
    op.create_index("ix_darkflow_interactions_status", "darkflow_interactions", ["status"])
    op.create_index("ix_darkflow_interactions_created_at", "darkflow_interactions", ["created_at"])
    op.create_index(
        "ix_darkflow_interactions_playbook_event",
        "darkflow_interactions",
        ["playbook", "event_ts"],
    )
    op.create_index(
        "ix_darkflow_interactions_symbol_timeframe_event",
        "darkflow_interactions",
        ["symbol", "timeframe", "event_ts"],
    )
    op.create_index(
        "ix_darkflow_interactions_status_event",
        "darkflow_interactions",
        ["status", "event_ts"],
    )


def downgrade() -> None:
    op.drop_table("darkflow_interactions")
    op.drop_table("darkflow_zones")
