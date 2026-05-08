"""baseline schema

Revision ID: 20260509_0001
Revises:
Create Date: 2026-05-09 00:01:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260509_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_snapshots",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("asset_tier", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("indicator", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("summary_payload", sa.JSON(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "price_snapshots",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("requested_assets", sa.JSON(), nullable=False),
        sa.Column("requested_timeframes", sa.JSON(), nullable=False),
        sa.Column("requested_indicators", sa.JSON(), nullable=False),
        sa.Column("snapshots_written", sa.Integer(), nullable=False),
        sa.Column("prices_written", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "strategy_decisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("strategy_name", sa.String(length=80), nullable=False),
        sa.Column("strategy_version", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("asset_tier", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.JSON(), nullable=False),
        sa.Column("risk_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "paper_trades",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("strategy_decision_id", sa.String(length=36), nullable=False),
        sa.Column("strategy_name", sa.String(length=80), nullable=False),
        sa.Column("strategy_version", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("asset_tier", sa.String(length=32), nullable=False),
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
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("strategy", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_assets", sa.JSON(), nullable=False),
        sa.Column("requested_timeframes", sa.JSON(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "signal_observations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("strategy_decision_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("asset_tier", sa.String(length=32), nullable=False),
        sa.Column("signal_name", sa.String(length=80), nullable=False),
        sa.Column("signal_role", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("strength", sa.Float(), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("price_at_signal", sa.Float(), nullable=True),
        sa.Column("strategy_decision", sa.String(length=32), nullable=False),
        sa.Column("strategy_score", sa.Float(), nullable=False),
        sa.Column("participated_in_score", sa.Boolean(), nullable=False),
        sa.Column("score_before", sa.Float(), nullable=False),
        sa.Column("score_after", sa.Float(), nullable=False),
        sa.Column("market_regime", sa.String(length=32), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    _create_indexes()


def downgrade() -> None:
    op.drop_table("signal_observations")
    op.drop_table("backtest_runs")
    op.drop_table("paper_trades")
    op.drop_table("strategy_decisions")
    op.drop_table("collection_runs")
    op.drop_table("price_snapshots")
    op.drop_table("signal_snapshots")


def _create_indexes() -> None:
    index_specs = [
        ("ix_signal_snapshots_symbol", "signal_snapshots", ["symbol"]),
        ("ix_signal_snapshots_asset_tier", "signal_snapshots", ["asset_tier"]),
        ("ix_signal_snapshots_timeframe", "signal_snapshots", ["timeframe"]),
        ("ix_signal_snapshots_interval", "signal_snapshots", ["interval"]),
        ("ix_signal_snapshots_indicator", "signal_snapshots", ["indicator"]),
        ("ix_signal_snapshots_collected_at", "signal_snapshots", ["collected_at"]),
        ("ix_signal_snapshots_created_at", "signal_snapshots", ["created_at"]),
        ("ix_price_snapshots_symbol", "price_snapshots", ["symbol"]),
        ("ix_price_snapshots_collected_at", "price_snapshots", ["collected_at"]),
        ("ix_price_snapshots_created_at", "price_snapshots", ["created_at"]),
        ("ix_collection_runs_status", "collection_runs", ["status"]),
        ("ix_strategy_decisions_strategy_name", "strategy_decisions", ["strategy_name"]),
        ("ix_strategy_decisions_strategy_version", "strategy_decisions", ["strategy_version"]),
        ("ix_strategy_decisions_symbol", "strategy_decisions", ["symbol"]),
        ("ix_strategy_decisions_asset_tier", "strategy_decisions", ["asset_tier"]),
        ("ix_strategy_decisions_direction", "strategy_decisions", ["direction"]),
        ("ix_strategy_decisions_score", "strategy_decisions", ["score"]),
        ("ix_strategy_decisions_decision", "strategy_decisions", ["decision"]),
        ("ix_strategy_decisions_created_at", "strategy_decisions", ["created_at"]),
        ("ix_paper_trades_strategy_decision_id", "paper_trades", ["strategy_decision_id"]),
        ("ix_paper_trades_strategy_name", "paper_trades", ["strategy_name"]),
        ("ix_paper_trades_strategy_version", "paper_trades", ["strategy_version"]),
        ("ix_paper_trades_symbol", "paper_trades", ["symbol"]),
        ("ix_paper_trades_asset_tier", "paper_trades", ["asset_tier"]),
        ("ix_paper_trades_direction", "paper_trades", ["direction"]),
        ("ix_paper_trades_status", "paper_trades", ["status"]),
        ("ix_backtest_runs_strategy", "backtest_runs", ["strategy"]),
        ("ix_backtest_runs_status", "backtest_runs", ["status"]),
        ("ix_backtest_runs_created_at", "backtest_runs", ["created_at"]),
        ("ix_signal_observations_strategy_decision_id", "signal_observations", ["strategy_decision_id"]),
        ("ix_signal_observations_symbol", "signal_observations", ["symbol"]),
        ("ix_signal_observations_asset_tier", "signal_observations", ["asset_tier"]),
        ("ix_signal_observations_signal_name", "signal_observations", ["signal_name"]),
        ("ix_signal_observations_signal_role", "signal_observations", ["signal_role"]),
        ("ix_signal_observations_direction", "signal_observations", ["direction"]),
        ("ix_signal_observations_timeframe", "signal_observations", ["timeframe"]),
        ("ix_signal_observations_strategy_decision", "signal_observations", ["strategy_decision"]),
        ("ix_signal_observations_market_regime", "signal_observations", ["market_regime"]),
        ("ix_signal_observations_status", "signal_observations", ["status"]),
        ("ix_signal_observations_observed_at", "signal_observations", ["observed_at"]),
        ("ix_signal_observations_created_at", "signal_observations", ["created_at"]),
        ("ix_signal_observations_updated_at", "signal_observations", ["updated_at"]),
        ("idx_signal_snapshots_lookup_latest", "signal_snapshots", ["symbol", "timeframe", "indicator", "created_at"]),
        ("idx_signal_snapshots_indicator_series", "signal_snapshots", ["indicator", "symbol", "timeframe", "created_at"]),
        ("idx_price_snapshots_symbol_collected", "price_snapshots", ["symbol", "collected_at"]),
        ("idx_collection_runs_started", "collection_runs", ["started_at"]),
        ("idx_strategy_decisions_symbol_created", "strategy_decisions", ["symbol", "created_at"]),
        ("idx_signal_observations_name_status_observed", "signal_observations", ["signal_name", "status", "observed_at"]),
        ("idx_signal_observations_role_status_observed", "signal_observations", ["signal_role", "status", "observed_at"]),
        ("idx_paper_trades_status_opened", "paper_trades", ["status", "opened_at"]),
    ]
    for name, table, columns in index_specs:
        op.create_index(name, table, columns, unique=False)
