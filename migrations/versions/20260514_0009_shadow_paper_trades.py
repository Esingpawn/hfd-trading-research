"""shadow paper trades

Revision ID: 20260514_0009
Revises: 20260514_0008
Create Date: 2026-05-14 00:09:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260514_0009"
down_revision = "20260514_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shadow_paper_trades",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("strategy_name", sa.String(length=120), nullable=False),
        sa.Column("candidate_type", sa.String(length=32), nullable=False),
        sa.Column("candidate_key", sa.String(length=240), nullable=False),
        sa.Column("signal_key", sa.String(length=120), nullable=False),
        sa.Column("source_experiment_run_id", sa.String(length=36), nullable=True),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("stop_loss", sa.Float(), nullable=False),
        sa.Column("take_profit", sa.Float(), nullable=False),
        sa.Column("position_size", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("exit_reason", sa.String(length=64), nullable=True),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("r_multiple", sa.Float(), nullable=True),
        sa.Column("mfe", sa.Float(), nullable=False),
        sa.Column("mae", sa.Float(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("signal_key", name="uq_shadow_paper_trades_signal_key"),
    )
    op.create_index("ix_shadow_paper_trades_strategy_name", "shadow_paper_trades", ["strategy_name"])
    op.create_index("ix_shadow_paper_trades_candidate_type", "shadow_paper_trades", ["candidate_type"])
    op.create_index("ix_shadow_paper_trades_candidate_key", "shadow_paper_trades", ["candidate_key"])
    op.create_index("ix_shadow_paper_trades_signal_key", "shadow_paper_trades", ["signal_key"])
    op.create_index("ix_shadow_paper_trades_source_experiment_run_id", "shadow_paper_trades", ["source_experiment_run_id"])
    op.create_index("ix_shadow_paper_trades_symbol", "shadow_paper_trades", ["symbol"])
    op.create_index("ix_shadow_paper_trades_timeframe", "shadow_paper_trades", ["timeframe"])
    op.create_index("ix_shadow_paper_trades_direction", "shadow_paper_trades", ["direction"])
    op.create_index("ix_shadow_paper_trades_status", "shadow_paper_trades", ["status"])
    op.create_index("ix_shadow_paper_trades_opened_at", "shadow_paper_trades", ["opened_at"])
    op.create_index(
        "ix_shadow_paper_trades_strategy_status",
        "shadow_paper_trades",
        ["strategy_name", "status"],
    )
    op.create_index(
        "ix_shadow_paper_trades_candidate",
        "shadow_paper_trades",
        ["candidate_type", "candidate_key"],
    )
    op.create_index(
        "ix_shadow_paper_trades_symbol_opened",
        "shadow_paper_trades",
        ["symbol", "opened_at"],
    )


def downgrade() -> None:
    op.drop_table("shadow_paper_trades")
