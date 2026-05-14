from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PriceSnapshot, SignalObservation, StrategyDecision, utc_now


HORIZONS: dict[str, timedelta] = {
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "24h": timedelta(hours=24),
}

SIGNAL_ROLES: dict[str, str] = {
    "长期方向": "direction",
    "中期结构": "structure",
    "短期触发": "trigger",
    "中期位置": "position",
    "短期位置": "position",
    "清算流动性": "risk",
    "订单流确认": "trigger",
    "趋势耗竭": "risk_filter",
    "全量研究覆盖": "data_quality",
    "盈亏比模板": "risk",
}


@dataclass(frozen=True)
class AttributionBackfillResult:
    updated: int
    pending: int
    skipped: int
    imported: int = 0


async def record_signal_observations(
    session: AsyncSession,
    decision: StrategyDecision,
    *,
    commit: bool = True,
) -> list[SignalObservation]:
    existing = await _existing_count(session, decision.id)
    if existing:
        return []

    observations = _observations_from_decision(decision)
    for item in observations:
        session.add(item)
    if observations and commit:
        await session.commit()
    return observations


async def backfill_signal_outcomes(
    session: AsyncSession,
    *,
    limit: int = 500,
    import_history: bool = True,
    commit: bool = True,
) -> AttributionBackfillResult:
    imported = 0
    if import_history:
        imported = await import_missing_signal_observations(
            session,
            limit=limit,
            commit=False,
        )
    rows = await session.execute(
        select(SignalObservation)
        .where(SignalObservation.status.in_(["pending", "partial"]))
        .order_by(SignalObservation.observed_at)
        .limit(limit)
    )
    observations = rows.scalars().all()
    updated = 0
    pending = 0
    skipped = 0
    for observation in observations:
        if observation.price_at_signal is None or observation.direction not in ("long", "short"):
            observation.status = "skipped"
            observation.updated_at = utc_now()
            skipped += 1
            continue
        labels = dict(observation.labels or {})
        complete = True
        for horizon, delta in HORIZONS.items():
            if f"return_{horizon}" in labels:
                continue
            price = await _price_at_or_after(session, observation.symbol, observation.observed_at + delta)
            if price is None:
                complete = False
                continue
            labels[f"return_{horizon}"] = _directional_return(
                observation.direction,
                float(observation.price_at_signal),
                price.price,
            )
            labels[f"price_{horizon}"] = price.price
            labels[f"price_{horizon}_at"] = price.collected_at.isoformat()
        labels.update(await _path_metrics(session, observation))
        observation.labels = labels
        observation.status = "labeled" if complete else "partial"
        observation.updated_at = utc_now()
        if complete:
            updated += 1
        else:
            pending += 1
    if commit and observations:
        await session.commit()
    elif commit and imported:
        await session.commit()
    return AttributionBackfillResult(updated=updated, pending=pending, skipped=skipped, imported=imported)


async def import_missing_signal_observations(
    session: AsyncSession,
    *,
    limit: int = 500,
    commit: bool = True,
) -> int:
    rows = await session.execute(
        select(StrategyDecision)
        .order_by(StrategyDecision.created_at.desc())
        .limit(limit)
    )
    imported = 0
    for decision in rows.scalars().all():
        observations = await record_signal_observations(session, decision, commit=False)
        imported += len(observations)
    if commit and imported:
        await session.commit()
    return imported


async def signal_effectiveness(
    session: AsyncSession,
    *,
    min_samples: int = 1,
    horizon: str = "4h",
) -> dict[str, Any]:
    rows = await session.execute(select(SignalObservation))
    observations = rows.scalars().all()
    labeled = [item for item in observations if _label_value(item, horizon) is not None]
    groups = _group_effectiveness(labeled, horizon=horizon, min_samples=min_samples)
    return {
        "horizon": horizon,
        "sample_count": len(observations),
        "labeled_count": len(labeled),
        "pending_count": len(observations) - len(labeled),
        "signals": groups,
        "roles": _group_effectiveness(labeled, horizon=horizon, min_samples=min_samples, key="signal_role"),
        "symbols": _group_effectiveness(labeled, horizon=horizon, min_samples=min_samples, key="symbol"),
    }


def _observations_from_decision(decision: StrategyDecision) -> list[SignalObservation]:
    risk = decision.risk_payload or {}
    reason = decision.reason or {}
    modules = risk.get("modules") or []
    price = (
        _float_or_none(risk.get("price_at_signal"))
        or _float_or_none(risk.get("entry_price"))
        or _float_or_none(risk.get("price"))
    )
    if price is None:
        price = _float_or_none(risk.get("entry_zone", {}).get("anchor"))
    rows = []
    score_before = 0.0
    for module in modules:
        name = str(module.get("name") or "unknown")
        points = float(module.get("points") or 0.0)
        score_after = score_before + points
        rows.append(
            SignalObservation(
                strategy_decision_id=decision.id,
                symbol=decision.symbol,
                asset_tier=decision.asset_tier,
                signal_name=name,
                signal_role=SIGNAL_ROLES.get(name, "research"),
                direction=decision.direction,
                strength=points,
                timeframe=_timeframe_for_module(name),
                interval=_interval_for_module(name),
                price_at_signal=price,
                strategy_decision=decision.decision,
                strategy_score=decision.score,
                participated_in_score=points > 0,
                score_before=score_before,
                score_after=score_after,
                market_regime=_market_regime(reason),
                context={
                    "module": module,
                    "rules": reason.get("rules") or [],
                    "stage": risk.get("opportunity_stage") or reason.get("opportunity_stage") or {},
                    "execution_gate": risk.get("execution_gate") or {},
                    "entry_plan": risk.get("entry_plan") or {},
                    "frozen_trade_plan": ((risk.get("entry_plan") or {}).get("frozen_snapshot") or {}),
                    "trade_levels": {
                        "entry_price": risk.get("entry_price"),
                        "stop_loss": risk.get("stop_loss"),
                        "take_profit": risk.get("take_profit"),
                        "risk_reward_ratio": (risk.get("entry_plan") or {}).get("risk_reward_ratio"),
                    },
                    "score_breakdown": risk.get("score_breakdown") or {},
                },
                observed_at=_aware(decision.created_at),
            )
        )
        score_before = score_after
    return rows


async def _existing_count(session: AsyncSession, decision_id: str) -> int:
    rows = await session.execute(
        select(func.count())
        .select_from(SignalObservation)
        .where(SignalObservation.strategy_decision_id == decision_id)
    )
    return int(rows.scalar_one())


async def _price_at_or_after(
    session: AsyncSession,
    symbol: str,
    target_at: datetime,
) -> PriceSnapshot | None:
    rows = await session.execute(
        select(PriceSnapshot)
        .where(PriceSnapshot.symbol == symbol, PriceSnapshot.collected_at >= target_at)
        .order_by(PriceSnapshot.collected_at)
        .limit(1)
    )
    return rows.scalar_one_or_none()


async def _path_metrics(
    session: AsyncSession,
    observation: SignalObservation,
) -> dict[str, Any]:
    end_at = observation.observed_at + HORIZONS["24h"]
    rows = await session.execute(
        select(PriceSnapshot)
        .where(
            PriceSnapshot.symbol == observation.symbol,
            PriceSnapshot.collected_at >= observation.observed_at,
            PriceSnapshot.collected_at <= end_at,
        )
        .order_by(PriceSnapshot.collected_at)
    )
    prices = rows.scalars().all()
    if not prices or observation.price_at_signal is None:
        return {}
    returns = [
        _directional_return(observation.direction, float(observation.price_at_signal), price.price)
        for price in prices
    ]
    metrics: dict[str, Any] = {"mfe": max(returns), "mae": min(returns)}
    levels = _tracking_levels(observation)
    if levels is None:
        return metrics
    first_hit = _first_level_hit(observation, prices, levels)
    metrics["path_24h"] = {
        "first_hit": first_hit["first_hit"],
        "first_hit_at": first_hit.get("first_hit_at"),
        "minutes_to_first_hit": first_hit.get("minutes_to_first_hit"),
        "stop_loss": levels["stop_loss"],
        "take_profit": levels["take_profit"],
        "entry_reference_price": levels.get("entry_reference_price"),
    }
    metrics["first_hit_24h"] = first_hit["first_hit"]
    metrics["hit_take_profit_before_stop_24h"] = first_hit["first_hit"] == "take_profit"
    metrics["path_outcome_24h"] = _path_outcome(first_hit["first_hit"], metrics["mfe"], metrics["mae"])
    return metrics


async def _mfe_mae(
    session: AsyncSession,
    observation: SignalObservation,
) -> tuple[float | None, float | None]:
    metrics = await _path_metrics(session, observation)
    return _float_or_none(metrics.get("mfe")), _float_or_none(metrics.get("mae"))


def _tracking_levels(observation: SignalObservation) -> dict[str, float] | None:
    context = observation.context or {}
    plan = context.get("entry_plan") if isinstance(context.get("entry_plan"), dict) else {}
    frozen = plan.get("frozen_snapshot") if isinstance(plan.get("frozen_snapshot"), dict) else {}
    levels = context.get("trade_levels") if isinstance(context.get("trade_levels"), dict) else {}
    stop_loss = _float_or_none(plan.get("stop_loss")) or _float_or_none(frozen.get("stop_loss")) or _float_or_none(levels.get("stop_loss"))
    take_profit = _float_or_none(plan.get("take_profit")) or _float_or_none(frozen.get("take_profit")) or _float_or_none(levels.get("take_profit"))
    if stop_loss is None or take_profit is None:
        return None
    return {
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "entry_reference_price": _float_or_none(plan.get("entry_reference_price"))
        or _float_or_none(frozen.get("entry_reference_price"))
        or _float_or_none(levels.get("entry_price"))
        or _float_or_none(observation.price_at_signal),
    }


def _first_level_hit(
    observation: SignalObservation,
    prices: list[PriceSnapshot],
    levels: dict[str, float],
) -> dict[str, Any]:
    for price in prices:
        if observation.direction == "short":
            hit = "stop_loss" if price.price >= levels["stop_loss"] else "take_profit" if price.price <= levels["take_profit"] else None
        else:
            hit = "stop_loss" if price.price <= levels["stop_loss"] else "take_profit" if price.price >= levels["take_profit"] else None
        if hit:
            return {
                "first_hit": hit,
                "first_hit_at": price.collected_at.isoformat(),
                "minutes_to_first_hit": round(max((_aware(price.collected_at) - _aware(observation.observed_at)).total_seconds() / 60, 0.0), 2),
            }
    return {"first_hit": "none"}


def _path_outcome(first_hit: str, mfe: float | None, mae: float | None) -> str:
    if first_hit == "take_profit":
        return "target_first"
    if first_hit == "stop_loss":
        return "stop_first"
    if mfe is not None and mae is not None and mfe > 0 and mae < 0:
        return "mixed_no_level_hit"
    if mfe is not None and mfe > 0:
        return "favorable_no_level_hit"
    if mae is not None and mae < 0:
        return "adverse_no_level_hit"
    return "flat_no_level_hit"


def _group_effectiveness(
    observations: list[SignalObservation],
    *,
    horizon: str,
    min_samples: int,
    key: str = "signal_name",
) -> list[dict[str, Any]]:
    buckets: dict[str, list[SignalObservation]] = {}
    for item in observations:
        buckets.setdefault(str(getattr(item, key)), []).append(item)
    rows = []
    for name, items in buckets.items():
        values = [_label_value(item, horizon) for item in items]
        values = [float(value) for value in values if value is not None]
        if len(values) < min_samples:
            continue
        wins = [value for value in values if value > 0]
        losses = [value for value in values if value < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        avg_return = sum(values) / len(values)
        role = items[0].signal_role if key == "signal_name" else None
        rows.append(
            {
                "name": name,
                "role": role,
                "sample_count": len(values),
                "win_rate": len(wins) / len(values),
                "avg_return": avg_return,
                "profit_factor": gross_win / gross_loss if gross_loss else (None if not gross_win else 999.0),
                "avg_mfe": _avg_label(items, "mfe"),
                "avg_mae": _avg_label(items, "mae"),
                "status": _signal_status(len(values), avg_return, len(wins) / len(values)),
            }
        )
    return sorted(rows, key=lambda row: (row["avg_return"], row["win_rate"], row["sample_count"]), reverse=True)


def _avg_label(items: list[SignalObservation], label: str) -> float | None:
    values = [item.labels.get(label) for item in items if isinstance(item.labels, dict)]
    numeric = [float(value) for value in values if isinstance(value, (int, float))]
    return sum(numeric) / len(numeric) if numeric else None


def _label_value(item: SignalObservation, horizon: str) -> float | None:
    labels = item.labels or {}
    value = labels.get(f"return_{horizon}")
    return float(value) if isinstance(value, (int, float)) else None


def _signal_status(sample_count: int, avg_return: float, win_rate: float) -> str:
    if sample_count < 30:
        return "observing"
    if avg_return > 0 and win_rate >= 0.52:
        return "promising"
    if avg_return < 0 and win_rate <= 0.48:
        return "noise_candidate"
    return "mixed"


def _directional_return(direction: str, entry: float, price: float) -> float:
    if not entry:
        return 0.0
    if direction == "short":
        return (entry - price) / entry
    return (price - entry) / entry


def _timeframe_for_module(name: str) -> str:
    if name.startswith("短期"):
        return "short"
    if name.startswith("中期"):
        return "mid"
    if name.startswith("长期"):
        return "long"
    return "strategy"


def _interval_for_module(name: str) -> str:
    return {"short": "30m", "mid": "1h", "long": "4h"}.get(_timeframe_for_module(name), "*")


def _market_regime(reason: dict[str, Any]) -> str:
    states = reason.get("states") or []
    biases = [state.get("bias") for state in states if isinstance(state, dict)]
    if len(set(bias for bias in biases if bias in ("long", "short"))) == 1 and len(biases) >= 3:
        return "aligned_trend"
    if any(bias in ("missing", "stale") for bias in biases):
        return "data_incomplete"
    return "mixed_structure"


def _float_or_none(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
