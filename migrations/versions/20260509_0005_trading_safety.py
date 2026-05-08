"""trading safety gateway

Revision ID: 20260509_0005
Revises: 20260509_0004
Create Date: 2026-05-09 00:05:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260509_0005"
down_revision = "20260509_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trading_safety_states",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("live_trading_enabled", sa.Boolean(), nullable=False),
        sa.Column("kill_switch_active", sa.Boolean(), nullable=False),
        sa.Column("manual_confirmation_required", sa.Boolean(), nullable=False),
        sa.Column("max_order_notional", sa.Float(), nullable=False),
        sa.Column("max_daily_notional", sa.Float(), nullable=False),
        sa.Column("max_daily_orders", sa.Integer(), nullable=False),
        sa.Column("allowed_symbols", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_trading_safety_states_scope", "trading_safety_states", ["scope"], unique=True)
    op.create_index("ix_trading_safety_states_created_at", "trading_safety_states", ["created_at"])
    op.create_index("ix_trading_safety_states_updated_at", "trading_safety_states", ["updated_at"])
    op.create_table(
        "trade_orders",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("order_type", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("requested_price", sa.Float(), nullable=True),
        sa.Column("notional", sa.Float(), nullable=True),
        sa.Column("strategy_decision_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("client_order_id", sa.String(length=120), nullable=True),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("safety_snapshot", sa.JSON(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_trade_orders_mode", "trade_orders", ["mode"])
    op.create_index("ix_trade_orders_symbol", "trade_orders", ["symbol"])
    op.create_index("ix_trade_orders_side", "trade_orders", ["side"])
    op.create_index("ix_trade_orders_order_type", "trade_orders", ["order_type"])
    op.create_index("ix_trade_orders_status", "trade_orders", ["status"])
    op.create_index("ix_trade_orders_strategy_decision_id", "trade_orders", ["strategy_decision_id"])
    op.create_index("ix_trade_orders_idempotency_key", "trade_orders", ["idempotency_key"], unique=True)
    op.create_index("ix_trade_orders_client_order_id", "trade_orders", ["client_order_id"])
    op.create_index("ix_trade_orders_created_at", "trade_orders", ["created_at"])
    op.create_table(
        "trading_audit_logs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("actor", sa.String(length=80), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=True),
        sa.Column("safety_state_id", sa.String(length=36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_trading_audit_logs_event_type", "trading_audit_logs", ["event_type"])
    op.create_index("ix_trading_audit_logs_actor", "trading_audit_logs", ["actor"])
    op.create_index("ix_trading_audit_logs_order_id", "trading_audit_logs", ["order_id"])
    op.create_index("ix_trading_audit_logs_safety_state_id", "trading_audit_logs", ["safety_state_id"])
    op.create_index("ix_trading_audit_logs_created_at", "trading_audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("trading_audit_logs")
    op.drop_table("trade_orders")
    op.drop_table("trading_safety_states")
