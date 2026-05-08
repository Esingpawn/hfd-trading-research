from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PaperTrade, PriceSnapshot, SignalObservation, StrategyDecision
from app.services.paper_stats import summarize_paper_trades


async def paper_trade_review(
    session: AsyncSession,
    trade_id: str,
) -> dict[str, Any] | None:
    trade = await _trade_by_id(session, trade_id)
    if trade is None:
        return None

    decision = await _decision_for_trade(session, trade)
    observations = await _observations_for_decision(
        session,
        decision.id if decision else trade.strategy_decision_id,
    )
    latest_price = await _latest_price(session, trade.symbol)
    sample_context = await _sample_context(session)
    current = _current_state(trade, latest_price.price if latest_price else None)

    return {
        "trade": _trade_payload(trade),
        "decision": _decision_payload(decision),
        "current": current,
        "sample_context": sample_context,
        "signals": [_observation_payload(item) for item in observations],
        "review": _review_payload(trade, decision, observations, current, sample_context),
    }


async def _trade_by_id(session: AsyncSession, trade_id: str) -> PaperTrade | None:
    rows = await session.execute(select(PaperTrade).where(PaperTrade.id == trade_id))
    return rows.scalar_one_or_none()


async def _decision_for_trade(
    session: AsyncSession,
    trade: PaperTrade,
) -> StrategyDecision | None:
    rows = await session.execute(
        select(StrategyDecision).where(StrategyDecision.id == trade.strategy_decision_id)
    )
    item = rows.scalar_one_or_none()
    if item is not None:
        return item

    rows = await session.execute(
        select(StrategyDecision)
        .where(
            StrategyDecision.symbol == trade.symbol,
            StrategyDecision.created_at <= trade.opened_at,
        )
        .order_by(StrategyDecision.created_at.desc())
        .limit(1)
    )
    return rows.scalar_one_or_none()


async def _observations_for_decision(
    session: AsyncSession,
    decision_id: str | None,
) -> list[SignalObservation]:
    if not decision_id:
        return []
    rows = await session.execute(
        select(SignalObservation)
        .where(SignalObservation.strategy_decision_id == decision_id)
        .order_by(SignalObservation.score_before, SignalObservation.signal_name)
    )
    return list(rows.scalars().all())


async def _latest_price(session: AsyncSession, symbol: str) -> PriceSnapshot | None:
    rows = await session.execute(
        select(PriceSnapshot)
        .where(PriceSnapshot.symbol == symbol)
        .order_by(PriceSnapshot.collected_at.desc(), PriceSnapshot.created_at.desc())
        .limit(1)
    )
    return rows.scalar_one_or_none()


async def _sample_context(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(select(PaperTrade).order_by(PaperTrade.opened_at))
    stats = summarize_paper_trades(list(rows.scalars().all()))
    return {
        "closed_trades": stats["closed_trades"],
        "total_trades": stats["total_trades"],
        "sample_ready": stats["sample_ready"],
        "sample_target": stats["sample_target"],
        "minimum_sample": stats["minimum_sample"],
        "sample_progress": stats["sample_progress"],
    }


def _trade_payload(trade: PaperTrade) -> dict[str, Any]:
    return {
        "id": trade.id,
        "strategy_decision_id": trade.strategy_decision_id,
        "strategy_name": trade.strategy_name,
        "strategy_version": trade.strategy_version,
        "symbol": trade.symbol,
        "asset_tier": trade.asset_tier,
        "direction": trade.direction,
        "entry_price": trade.entry_price,
        "stop_loss": trade.stop_loss,
        "take_profit": trade.take_profit,
        "position_size": trade.position_size,
        "status": trade.status,
        "exit_price": trade.exit_price,
        "exit_reason": trade.exit_reason,
        "pnl": trade.pnl,
        "r_multiple": trade.r_multiple,
        "mfe": trade.mfe,
        "mae": trade.mae,
        "opened_at": trade.opened_at,
        "closed_at": trade.closed_at,
        "duration_minutes": _duration_minutes(trade.opened_at, trade.closed_at),
    }


def _decision_payload(decision: StrategyDecision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    risk = decision.risk_payload or {}
    return {
        "id": decision.id,
        "strategy": decision.strategy_name,
        "version": decision.strategy_version,
        "symbol": decision.symbol,
        "asset_tier": decision.asset_tier,
        "direction": decision.direction,
        "score": decision.score,
        "decision": decision.decision,
        "reason": decision.reason,
        "risk": risk,
        "score_breakdown": risk.get("score_breakdown") or {},
        "created_at": decision.created_at,
    }


def _current_state(trade: PaperTrade, latest_price: float | None) -> dict[str, Any]:
    mark_price = trade.exit_price if trade.status == "closed" and trade.exit_price else latest_price
    return_pct = (
        _directional_return(trade.direction, trade.entry_price, mark_price)
        if mark_price is not None
        else None
    )
    return {
        "mark_price": mark_price,
        "latest_price": latest_price,
        "return_pct": return_pct,
        "position_pnl": return_pct * trade.position_size if return_pct is not None else None,
        "distance_to_stop_pct": _distance_to_stop_pct(trade, mark_price),
        "distance_to_target_pct": _distance_to_target_pct(trade, mark_price),
        "outcome_status": _outcome_status(trade, return_pct),
    }


def _observation_payload(observation: SignalObservation) -> dict[str, Any]:
    labels = observation.labels or {}
    return_4h = _float_or_none(labels.get("return_4h"))
    return {
        "id": observation.id,
        "signal_name": observation.signal_name,
        "signal_role": observation.signal_role,
        "direction": observation.direction,
        "strength": observation.strength,
        "timeframe": observation.timeframe,
        "interval": observation.interval,
        "strategy_decision": observation.strategy_decision,
        "strategy_score": observation.strategy_score,
        "participated_in_score": observation.participated_in_score,
        "status": observation.status,
        "market_regime": observation.market_regime,
        "returns": {
            "30m": _float_or_none(labels.get("return_30m")),
            "1h": _float_or_none(labels.get("return_1h")),
            "4h": return_4h,
            "24h": _float_or_none(labels.get("return_24h")),
        },
        "mfe": _float_or_none(labels.get("mfe")),
        "mae": _float_or_none(labels.get("mae")),
        "effect_4h": _signal_effect(return_4h),
        "context": observation.context or {},
        "observed_at": observation.observed_at,
    }


def _review_payload(
    trade: PaperTrade,
    decision: StrategyDecision | None,
    observations: list[SignalObservation],
    current: dict[str, Any],
    sample_context: dict[str, Any],
) -> dict[str, Any]:
    helpful = 0
    harmful = 0
    pending = 0
    for item in observations:
        effect = _signal_effect(_float_or_none((item.labels or {}).get("return_4h")))
        if effect == "helpful":
            helpful += 1
        elif effect == "harmful":
            harmful += 1
        else:
            pending += 1

    risk = decision.risk_payload if decision else {}
    score_breakdown = (risk or {}).get("score_breakdown") or {}
    return {
        "outcome_status": current.get("outcome_status"),
        "sample_status": "ready" if sample_context.get("sample_ready") else "insufficient",
        "trade_status": trade.status,
        "signal_counts": {
            "total": len(observations),
            "helpful_4h": helpful,
            "harmful_4h": harmful,
            "pending_4h": pending,
        },
        "entry_quality": _entry_quality(trade, current),
        "decision_quality": _decision_quality(decision, harmful, helpful),
        "weight_mode": score_breakdown.get("weight_mode") or "baseline",
        "base_score": decision.score if decision else None,
        "weighted_score": score_breakdown.get("weighted_score"),
        "next_actions": _next_actions(trade, sample_context, pending, harmful),
    }


def _directional_return(direction: str, entry: float, price: float | None) -> float | None:
    if price is None or not entry:
        return None
    if direction == "short":
        return (entry - price) / entry
    return (price - entry) / entry


def _distance_to_stop_pct(trade: PaperTrade, mark_price: float | None) -> float | None:
    if mark_price is None or not mark_price:
        return None
    if trade.direction == "short":
        return (trade.stop_loss - mark_price) / mark_price
    return (mark_price - trade.stop_loss) / mark_price


def _distance_to_target_pct(trade: PaperTrade, mark_price: float | None) -> float | None:
    if mark_price is None or not mark_price:
        return None
    if trade.direction == "short":
        return (mark_price - trade.take_profit) / mark_price
    return (trade.take_profit - mark_price) / mark_price


def _outcome_status(trade: PaperTrade, return_pct: float | None) -> str:
    if trade.status == "closed":
        if trade.exit_reason == "take_profit" or (return_pct is not None and return_pct > 0):
            return "closed_win"
        if trade.exit_reason == "stop_loss" or (return_pct is not None and return_pct < 0):
            return "closed_loss"
        return "closed_flat"
    if return_pct is None:
        return "open_unknown"
    if return_pct > 0:
        return "open_profitable"
    if return_pct < 0:
        return "open_drawdown"
    return "open_flat"


def _signal_effect(value: float | None) -> str:
    if value is None:
        return "pending"
    if value > 0:
        return "helpful"
    if value < 0:
        return "harmful"
    return "flat"


def _entry_quality(trade: PaperTrade, current: dict[str, Any]) -> str:
    if trade.status == "closed":
        return "resolved"
    distance_to_stop = current.get("distance_to_stop_pct")
    distance_to_target = current.get("distance_to_target_pct")
    if distance_to_stop is not None and distance_to_stop <= 0:
        return "stop_reached"
    if distance_to_target is not None and distance_to_target <= 0:
        return "target_reached"
    current_return = current.get("return_pct")
    if current_return is None:
        return "unknown"
    if current_return > 0:
        return "working"
    if current_return < 0:
        return "under_pressure"
    return "flat"


def _decision_quality(
    decision: StrategyDecision | None,
    harmful_signals: int,
    helpful_signals: int,
) -> str:
    if decision is None:
        return "missing_decision"
    if harmful_signals > helpful_signals:
        return "review_required"
    if helpful_signals > harmful_signals:
        return "supported"
    return "unresolved"


def _next_actions(
    trade: PaperTrade,
    sample_context: dict[str, Any],
    pending_signals: int,
    harmful_signals: int,
) -> list[str]:
    actions: list[str] = []
    if not sample_context.get("sample_ready"):
        actions.append("accumulate_samples")
    if pending_signals:
        actions.append("backfill_attribution")
    if trade.status == "open":
        actions.append("mark_open_trade")
    if harmful_signals:
        actions.append("review_harmful_signals")
    if not actions:
        actions.append("keep_monitoring")
    return actions


def _duration_minutes(start: datetime, end: datetime | None) -> float | None:
    if not end:
        return None
    return round(max((_aware(end) - _aware(start)).total_seconds() / 60, 0), 2)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _float_or_none(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
