"""darkflow trade candidates

Revision ID: 20260516_0012
Revises: 20260516_0011
Create Date: 2026-05-16 15:20:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260516_0012"
down_revision = "20260516_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_candidates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("candidate_key", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_interaction_id", sa.String(length=36), nullable=True),
        sa.Column("lineage", sa.String(length=64), nullable=False),
        sa.Column("strategy_family", sa.String(length=80), nullable=False),
        sa.Column("strategy_id", sa.String(length=80), nullable=False),
        sa.Column("strategy_name", sa.String(length=120), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("setup_type", sa.String(length=64), nullable=False),
        sa.Column("market_state", sa.String(length=80), nullable=False),
        sa.Column("setup_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("stop_price", sa.Float(), nullable=False),
        sa.Column("target_price", sa.Float(), nullable=False),
        sa.Column("rr_ratio", sa.Float(), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("rule_score", sa.Float(), nullable=False),
        sa.Column("model_win_prob", sa.Float(), nullable=True),
        sa.Column("expected_r", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("promotion_status", sa.String(length=32), nullable=False),
        sa.Column("anti_repaint_status", sa.String(length=32), nullable=False),
        sa.Column("shadow_status", sa.String(length=32), nullable=False),
        sa.Column("paper_eligible", sa.Boolean(), nullable=False),
        sa.Column("live_eligible", sa.Boolean(), nullable=False),
        sa.Column("blockers", sa.JSON(), nullable=False),
        sa.Column("promotion_blockers", sa.JSON(), nullable=False),
        sa.Column("supporting_signals", sa.JSON(), nullable=False),
        sa.Column("decision_payload", sa.JSON(), nullable=False),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("candidate_key", name="uq_trade_candidates_candidate_key"),
    )
    op.create_index("ix_trade_candidates_candidate_key", "trade_candidates", ["candidate_key"], unique=True)
    op.create_index("ix_trade_candidates_source_interaction_id", "trade_candidates", ["source_interaction_id"])
    op.create_index("ix_trade_candidates_lineage", "trade_candidates", ["lineage"])
    op.create_index("ix_trade_candidates_strategy_family", "trade_candidates", ["strategy_family"])
    op.create_index("ix_trade_candidates_strategy_id", "trade_candidates", ["strategy_id"])
    op.create_index("ix_trade_candidates_symbol", "trade_candidates", ["symbol"])
    op.create_index("ix_trade_candidates_timeframe", "trade_candidates", ["timeframe"])
    op.create_index("ix_trade_candidates_interval", "trade_candidates", ["interval"])
    op.create_index("ix_trade_candidates_direction", "trade_candidates", ["direction"])
    op.create_index("ix_trade_candidates_setup_type", "trade_candidates", ["setup_type"])
    op.create_index("ix_trade_candidates_market_state", "trade_candidates", ["market_state"])
    op.create_index("ix_trade_candidates_setup_time", "trade_candidates", ["setup_time"])
    op.create_index("ix_trade_candidates_status", "trade_candidates", ["status"])
    op.create_index("ix_trade_candidates_promotion_status", "trade_candidates", ["promotion_status"])
    op.create_index("ix_trade_candidates_anti_repaint_status", "trade_candidates", ["anti_repaint_status"])
    op.create_index("ix_trade_candidates_shadow_status", "trade_candidates", ["shadow_status"])
    op.create_index("ix_trade_candidates_materialized_at", "trade_candidates", ["materialized_at"])
    op.create_index("ix_trade_candidates_updated_at", "trade_candidates", ["updated_at"])
    op.create_index("ix_trade_candidates_lineage_status", "trade_candidates", ["lineage", "status"])
    op.create_index("ix_trade_candidates_symbol_status", "trade_candidates", ["symbol", "status"])
    op.create_index("ix_trade_candidates_strategy_setup", "trade_candidates", ["strategy_id", "setup_time"])


def downgrade() -> None:
    op.drop_table("trade_candidates")
