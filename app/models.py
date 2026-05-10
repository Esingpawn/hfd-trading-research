from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Float, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def uuid_str() -> str:
    return str(uuid.uuid4())


class SignalSnapshot(Base):
    __tablename__ = "signal_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    asset_tier: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(16), index=True)
    interval: Mapped[str] = mapped_column(String(8), index=True)
    indicator: Mapped[str] = mapped_column(String(64), index=True)
    endpoint: Mapped[str] = mapped_column(Text)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    raw_payload_uri: Mapped[str | None] = mapped_column(Text)
    raw_payload_sha256: Mapped[str | None] = mapped_column(String(64))
    raw_payload_bytes: Mapped[int | None] = mapped_column(Integer)
    raw_payload_compression: Mapped[str | None] = mapped_column(String(16))
    summary_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    price: Mapped[float] = mapped_column(Float)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class FeatureEvent(Base):
    __tablename__ = "feature_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_feature_events_event_key"),
        Index("ix_feature_events_indicator_feature_ts", "indicator", "feature_name", "event_ts"),
        Index("ix_feature_events_symbol_timeframe_ts", "symbol", "timeframe", "event_ts"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    snapshot_id: Mapped[str] = mapped_column(String(36), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    asset_tier: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(16), index=True)
    interval: Mapped[str] = mapped_column(String(8), index=True)
    indicator: Mapped[str] = mapped_column(String(64), index=True)
    event_key: Mapped[str] = mapped_column(String(64), index=True)
    feature_name: Mapped[str] = mapped_column(String(120), index=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event_price: Mapped[float | None] = mapped_column(Float)
    strength: Mapped[float] = mapped_column(Float, default=0.0)
    subtype: Mapped[str] = mapped_column(String(80), index=True, default="unknown")
    source_payload_key: Mapped[str] = mapped_column(String(120), index=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class FeatureLabel(Base):
    __tablename__ = "feature_labels"
    __table_args__ = (
        UniqueConstraint("feature_event_id", "horizon", name="uq_feature_labels_event_horizon"),
        Index("ix_feature_labels_horizon_status", "horizon", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    feature_event_id: Mapped[str] = mapped_column(String(36), index=True)
    horizon: Mapped[str] = mapped_column(String(16), index=True)
    return_pct: Mapped[float | None] = mapped_column(Float)
    mfe: Mapped[float | None] = mapped_column(Float)
    mae: Mapped[float | None] = mapped_column(Float)
    future_price: Mapped[float | None] = mapped_column(Float)
    future_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    status: Mapped[str] = mapped_column(String(32), index=True)
    dry_run: Mapped[bool] = mapped_column(default=False)
    requested_assets: Mapped[list[str]] = mapped_column(JSON)
    requested_timeframes: Mapped[list[str]] = mapped_column(JSON)
    requested_indicators: Mapped[list[str]] = mapped_column(JSON)
    snapshots_written: Mapped[int] = mapped_column(Integer, default=0)
    prices_written: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyDecision(Base):
    __tablename__ = "strategy_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    strategy_name: Mapped[str] = mapped_column(String(80), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    asset_tier: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)
    score: Mapped[float] = mapped_column(Float, index=True)
    decision: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[dict[str, Any]] = mapped_column(JSON)
    risk_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    strategy_decision_id: Mapped[str] = mapped_column(String(36), index=True)
    strategy_name: Mapped[str] = mapped_column(String(80), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    asset_tier: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    position_size: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), index=True, default="open")
    exit_price: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[str | None] = mapped_column(String(64))
    pnl: Mapped[float | None] = mapped_column(Float)
    r_multiple: Mapped[float | None] = mapped_column(Float)
    mfe: Mapped[float] = mapped_column(Float, default=0.0)
    mae: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    strategy: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    requested_assets: Mapped[list[str]] = mapped_column(JSON)
    requested_timeframes: Mapped[list[str]] = mapped_column(JSON)
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class SignalObservation(Base):
    __tablename__ = "signal_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    strategy_decision_id: Mapped[str] = mapped_column(String(36), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    asset_tier: Mapped[str] = mapped_column(String(32), index=True)
    signal_name: Mapped[str] = mapped_column(String(80), index=True)
    signal_role: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)
    strength: Mapped[float] = mapped_column(Float, default=0.0)
    timeframe: Mapped[str] = mapped_column(String(16), index=True, default="strategy")
    interval: Mapped[str] = mapped_column(String(8), default="*")
    price_at_signal: Mapped[float | None] = mapped_column(Float)
    strategy_decision: Mapped[str] = mapped_column(String(32), index=True)
    strategy_score: Mapped[float] = mapped_column(Float, default=0.0)
    participated_in_score: Mapped[bool] = mapped_column(default=True)
    score_before: Mapped[float] = mapped_column(Float, default=0.0)
    score_after: Mapped[float] = mapped_column(Float, default=0.0)
    market_regime: Mapped[str] = mapped_column(String(32), index=True, default="unknown")
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    labels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    task_name: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="queued")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExperimentRun(Base):
    __tablename__ = "experiment_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft")
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class WeightVersion(Base):
    __tablename__ = "weight_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft")
    weights: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class TradingSafetyState(Base):
    __tablename__ = "trading_safety_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    scope: Mapped[str] = mapped_column(String(64), unique=True, index=True, default="global")
    live_trading_enabled: Mapped[bool] = mapped_column(default=False)
    kill_switch_active: Mapped[bool] = mapped_column(default=True)
    manual_confirmation_required: Mapped[bool] = mapped_column(default=True)
    max_order_notional: Mapped[float] = mapped_column(Float, default=0.0)
    max_daily_notional: Mapped[float] = mapped_column(Float, default=0.0)
    max_daily_orders: Mapped[int] = mapped_column(Integer, default=0)
    allowed_symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class TradeOrder(Base):
    __tablename__ = "trade_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    mode: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    side: Mapped[str] = mapped_column(String(16), index=True)
    order_type: Mapped[str] = mapped_column(String(24), index=True, default="market")
    status: Mapped[str] = mapped_column(String(32), index=True, default="requested")
    quantity: Mapped[float] = mapped_column(Float)
    requested_price: Mapped[float | None] = mapped_column(Float)
    notional: Mapped[float | None] = mapped_column(Float)
    strategy_decision_id: Mapped[str | None] = mapped_column(String(36), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True, index=True)
    client_order_id: Mapped[str | None] = mapped_column(String(120), index=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    safety_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TradingAuditLog(Base):
    __tablename__ = "trading_audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    actor: Mapped[str] = mapped_column(String(80), index=True, default="system")
    order_id: Mapped[str | None] = mapped_column(String(36), index=True)
    safety_state_id: Mapped[str | None] = mapped_column(String(36), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
