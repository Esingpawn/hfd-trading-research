from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import ASSETS, CORE_INDICATORS, REQUIRED_SCORING_INDICATORS, TIMEFRAMES
from app.models import PriceSnapshot, SignalSnapshot, StrategyDecision
from app.services.completeness import _as_aware, _is_stale
from app.services.raw_payloads import payload_for_snapshot
from app.services.risk import build_trade_levels, template_for_tier


STRATEGY_NAME = "multi_timeframe_cost_stack"
STRATEGY_VERSION = "0.3"
ENTRY_PLAN_VALID_MINUTES = 35
ENTRY_PLAN_DRIFT_LIMIT_PCT = 0.003


@dataclass(frozen=True)
class TimeframeState:
    timeframe: str
    interval: str
    bias: str
    avg_price: float | None
    distance_pct: float | None
    status: str | None
    snapshot_id: str | None
    is_stale: bool = False
    age_minutes: float | None = None


@dataclass(frozen=True)
class StrategyEvaluation:
    symbol: str
    asset_tier: str
    direction: str
    score: float
    decision: str
    reason: dict[str, Any]
    risk_payload: dict[str, Any]
    price: float | None
    states: list[TimeframeState] = field(default_factory=list)
    modules: list[dict[str, Any]] = field(default_factory=list)


async def evaluate_symbol(
    session: AsyncSession,
    coin: str,
    dry_run: bool = False,
) -> StrategyEvaluation:
    from app.services.signal_weights import build_signal_weight_map

    coin = coin.upper()
    asset = ASSETS[coin]
    symbol = f"{coin}USDT"
    price = await _latest_price(session, symbol)
    states: list[TimeframeState] = []
    snapshots_by_indicator: dict[str, SignalSnapshot] = {}
    stale_indicators: list[str] = []

    for timeframe_name, timeframe in TIMEFRAMES.items():
        snapshot = await _latest_snapshot(
            session,
            symbol=symbol,
            timeframe=timeframe_name,
            indicator="smart_money_cost",
        )
        states.append(_state_from_snapshot(snapshot, timeframe_name, timeframe.interval, price))

    for indicator in CORE_INDICATORS:
        snapshot = await _latest_snapshot_any_timeframe(
            session,
            symbol=symbol,
            indicator=indicator,
        )
        if snapshot is None:
            continue
        if _snapshot_is_fresh(snapshot):
            snapshots_by_indicator[indicator] = snapshot
        else:
            stale_indicators.append(indicator)

    weight_map = await build_signal_weight_map(session)
    evaluation = _score_states(
        symbol,
        asset.tier,
        price,
        states,
        snapshots_by_indicator,
        stale_indicators=stale_indicators,
        signal_weights=weight_map,
    )
    evaluation.risk_payload["price_at_signal"] = price
    if not dry_run:
        from app.services.signal_attribution import record_signal_observations

        decision = StrategyDecision(
            strategy_name=STRATEGY_NAME,
            strategy_version=STRATEGY_VERSION,
            symbol=evaluation.symbol,
            asset_tier=evaluation.asset_tier,
            direction=evaluation.direction,
            score=evaluation.score,
            decision=evaluation.decision,
            reason=evaluation.reason,
            risk_payload=evaluation.risk_payload,
        )
        session.add(
            decision
        )
        await session.flush()
        await record_signal_observations(session, decision, commit=False)
        await session.commit()
    return evaluation


async def _latest_price(session: AsyncSession, symbol: str) -> float | None:
    rows = await session.execute(
        select(PriceSnapshot)
        .where(PriceSnapshot.symbol == symbol)
        .order_by(PriceSnapshot.created_at.desc())
        .limit(1)
    )
    item = rows.scalar_one_or_none()
    return item.price if item else None


async def _latest_snapshot(
    session: AsyncSession,
    symbol: str,
    timeframe: str,
    indicator: str,
) -> SignalSnapshot | None:
    rows = await session.execute(
        select(SignalSnapshot)
        .where(
            SignalSnapshot.symbol == symbol,
            SignalSnapshot.timeframe == timeframe,
            SignalSnapshot.indicator == indicator,
        )
        .order_by(SignalSnapshot.created_at.desc())
        .limit(1)
    )
    return rows.scalar_one_or_none()


async def _latest_snapshot_any_timeframe(
    session: AsyncSession,
    symbol: str,
    indicator: str,
) -> SignalSnapshot | None:
    rows = await session.execute(
        select(SignalSnapshot)
        .where(
            SignalSnapshot.symbol == symbol,
            SignalSnapshot.indicator == indicator,
        )
        .order_by(SignalSnapshot.created_at.desc())
        .limit(1)
    )
    return rows.scalar_one_or_none()


def _state_from_snapshot(
    snapshot: SignalSnapshot | None,
    timeframe: str,
    interval: str,
    price: float | None,
) -> TimeframeState:
    if snapshot is None:
        return TimeframeState(timeframe, interval, "missing", None, None, None, None)
    is_stale, age_minutes = _snapshot_staleness(snapshot)
    if is_stale:
        return TimeframeState(
            timeframe,
            interval,
            "stale",
            None,
            None,
            "stale",
            snapshot.id,
            True,
            age_minutes,
        )
    zones = payload_for_snapshot(snapshot).get("smart_money_cost") or []
    if not zones:
        return TimeframeState(timeframe, interval, "empty", None, None, None, snapshot.id)
    zone = zones[-1]
    avg_price = float(zone["avg_price"])
    bias = _bias_from_zone(zone)
    distance = abs(price - avg_price) / avg_price if price else None
    return TimeframeState(
        timeframe=timeframe,
        interval=interval,
        bias=bias,
        avg_price=avg_price,
        distance_pct=distance,
        status=zone.get("status"),
        snapshot_id=snapshot.id,
    )


def _bias_from_zone(zone: dict[str, Any]) -> str:
    zone_type = str(zone.get("type", "")).lower()
    if zone_type == "accumulation":
        return "long"
    if zone_type == "distribution":
        return "short"
    return "neutral"


def _score_states(
    symbol: str,
    asset_tier: str,
    price: float | None,
    states: list[TimeframeState],
    snapshots_by_indicator: dict[str, SignalSnapshot] | None = None,
    stale_indicators: list[str] | None = None,
    signal_weights: dict[str, dict[str, Any]] | None = None,
) -> StrategyEvaluation:
    snapshots_by_indicator = snapshots_by_indicator or {}
    signal_weights = signal_weights or {}
    stale_indicators = sorted(set(stale_indicators or []))
    by_timeframe = {state.timeframe: state for state in states}
    long_state = by_timeframe.get("long")
    mid_state = by_timeframe.get("mid")
    short_state = by_timeframe.get("short")
    template = template_for_tier(asset_tier)
    reason: dict[str, Any] = {
        "states": [state.__dict__ for state in states],
        "rules": [],
        "explanation": [],
        "warnings": [],
        "missing_indicators": [
            indicator for indicator in CORE_INDICATORS if indicator not in snapshots_by_indicator
        ],
        "stale_indicators": stale_indicators,
    }
    modules: list[dict[str, Any]] = []

    if price is None:
        return StrategyEvaluation(
            symbol,
            asset_tier,
            "none",
            0,
            "observe",
            {"error": "missing_price", "explanation": ["缺少最新价格，禁止评分。"]},
            {},
            price,
            states,
            modules,
        )
    if long_state is None or long_state.bias not in ("long", "short"):
        stale_timeframes = [state.timeframe for state in states if state.is_stale]
        explanation = "缺少长期方向或长期数据已过期，禁止开仓。"
        if stale_timeframes:
            explanation += f" 过期周期：{', '.join(stale_timeframes)}。"
        return StrategyEvaluation(
            symbol,
            asset_tier,
            "none",
            0,
            "observe",
            {
                "error": "missing_or_stale_long_bias",
                "explanation": [explanation],
                "missing_indicators": reason["missing_indicators"],
                "stale_indicators": stale_indicators,
            },
            {},
            price,
            states,
            modules,
        )

    direction = long_state.bias
    score = 0.0
    score += _add_module(modules, "长期方向", 25, f"长期趋势成本带偏{_cn_direction(direction)}。", "ok")
    reason["rules"].append("long_term_direction")
    reason["explanation"].append(f"长期方向偏{_cn_direction(direction)}，这是本次判断的主方向。")

    if mid_state and mid_state.bias == direction:
        score += _add_module(modules, "中期结构", 15, "中期方向与长期一致。", "ok")
        reason["rules"].append("mid_term_aligned")
    else:
        score += _add_module(modules, "中期结构", 0, "中期方向缺失或不一致。", "warn")
        reason["warnings"].append("中期结构未确认。")
    if short_state and short_state.bias == direction:
        score += _add_module(modules, "短期触发", 10, "短期方向与长期一致。", "ok")
        reason["rules"].append("short_term_aligned")
    else:
        score += _add_module(modules, "短期触发", 0, "短期方向缺失或不一致。", "warn")
        reason["warnings"].append("短期触发未确认。")

    if mid_state and mid_state.distance_pct is not None and mid_state.distance_pct <= _distance_limit(asset_tier, "mid"):
        score += _add_module(modules, "中期位置", 10, "价格靠近中期成本区。", "ok")
        reason["rules"].append("mid_term_near_cost")
    else:
        score += _add_module(modules, "中期位置", 0, "价格未靠近中期成本区。", "warn")
    if short_state and short_state.distance_pct is not None and short_state.distance_pct <= _distance_limit(asset_tier, "short"):
        score += _add_module(modules, "短期位置", 10, "价格靠近短期成本区。", "ok")
        reason["rules"].append("short_term_near_cost")

    score += _score_liquidity_module(modules, snapshots_by_indicator, reason)
    score += _score_orderflow_module(modules, snapshots_by_indicator, reason)
    score += _score_exhaustion_module(modules, snapshots_by_indicator, reason)
    score += _score_data_quality_module(modules, snapshots_by_indicator, reason)

    score += _add_module(modules, "盈亏比模板", 10, "风控模板给出至少 1:2 的基础盈亏比。", "ok")
    reason["rules"].append("risk_reward_template_available")
    risk_payload = build_trade_levels(
        direction,
        price,
        asset_tier,
        states=states,
        snapshots=snapshots_by_indicator,
    )
    risk_payload["min_score"] = template.min_score
    risk_payload["modules"] = modules
    execution = _execution_gate(asset_tier, direction, price, short_state, mid_state, risk_payload)
    risk_payload["entry_zone"] = execution["entry_zone"]
    risk_payload["execution_zone"] = execution["execution_zone"]
    risk_payload["entry_plan"] = _entry_plan(
        symbol=symbol,
        direction=direction,
        price=price,
        asset_tier=asset_tier,
        execution_zone=execution["execution_zone"],
        entry_zone=execution["entry_zone"],
        stop_loss=risk_payload.get("stop_loss"),
        take_profit=risk_payload.get("take_profit"),
    )
    risk_payload["execution_gate"] = {
        "ready": execution["ready"],
        "reasons": execution["reasons"],
        "warnings": execution["warnings"],
    }

    required_missing = _missing_required_indicators(snapshots_by_indicator)
    reason["required_missing_indicators"] = required_missing
    if required_missing:
        reason["warnings"].append(f"核心指标缺失：{', '.join(required_missing)}。")
    if stale_indicators:
        reason["warnings"].append(f"核心指标已过期：{', '.join(stale_indicators)}。")
    if not execution["ready"]:
        reason["warnings"].extend(execution["warnings"])
    weighted_score = _weighted_score(modules, signal_weights)
    decision = "open" if score >= template.min_score and not required_missing and execution["ready"] else "observe"
    stage = _opportunity_stage(score, template.min_score, required_missing, risk_payload)
    risk_payload["score_breakdown"] = _score_breakdown(
        score=score,
        min_score=template.min_score,
        modules=modules,
        execution_ready=execution["ready"],
        execution_zone=execution["execution_zone"],
        required_missing=required_missing,
        weighted_score=weighted_score,
        signal_weights=signal_weights,
    )
    reason["opportunity_stage"] = stage
    risk_payload["opportunity_stage"] = stage
    return StrategyEvaluation(
        symbol=symbol,
        asset_tier=asset_tier,
        direction=direction,
        score=score,
        decision=decision,
        reason=reason,
        risk_payload=risk_payload,
        price=price,
        states=states,
        modules=modules,
    )


def _distance_limit(asset_tier: str, timeframe: str) -> float:
    if asset_tier == "core":
        return 0.012 if timeframe == "short" else 0.018
    if asset_tier == "mainstream":
        return 0.018 if timeframe == "short" else 0.025
    return 0.028 if timeframe == "short" else 0.04


def _entry_plan(
    *,
    symbol: str,
    direction: str,
    price: float,
    asset_tier: str,
    execution_zone: dict[str, Any],
    entry_zone: dict[str, Any],
    stop_loss: Any,
    take_profit: Any,
) -> dict[str, Any]:
    created_at = datetime.now(timezone.utc)
    valid_until = created_at + timedelta(minutes=ENTRY_PLAN_VALID_MINUTES)
    reference = _entry_reference_price(direction, price, execution_zone)
    risk_reward = _risk_reward_ratio(direction, reference, stop_loss, take_profit)
    frozen_snapshot = {
        "symbol": symbol,
        "direction": direction,
        "asset_tier": asset_tier,
        "signal_price": price,
        "entry_reference_price": reference,
        "entry_range": {"lower": execution_zone.get("lower"), "upper": execution_zone.get("upper")},
        "cost_range": {"lower": entry_zone.get("lower"), "upper": entry_zone.get("upper")},
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_reward_ratio": risk_reward,
    }
    return {
        "kind": "snapshot_trade_plan",
        "symbol": symbol,
        "direction": direction,
        "status": "active" if execution_zone.get("valid") else "invalid",
        "created_at": created_at.isoformat(),
        "valid_until": valid_until.isoformat(),
        "valid_for_minutes": ENTRY_PLAN_VALID_MINUTES,
        "drift_limit_pct": ENTRY_PLAN_DRIFT_LIMIT_PCT,
        "signal_price": price,
        "entry_reference_price": reference,
        "entry_lower": execution_zone.get("lower"),
        "entry_upper": execution_zone.get("upper"),
        "entry_range": frozen_snapshot["entry_range"],
        "cost_lower": entry_zone.get("lower"),
        "cost_upper": entry_zone.get("upper"),
        "cost_range": frozen_snapshot["cost_range"],
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_reward_ratio": risk_reward,
        "frozen_snapshot": frozen_snapshot,
        "used_for_outcome_tracking": True,
        "plan_key": _plan_key(direction, reference, execution_zone, stop_loss, take_profit),
        "invalidates_when": [
            "price leaves entry_lower/entry_upper",
            "new scan changes direction",
            "new scan changes entry/stop/target beyond drift_limit_pct",
            "valid_until passes before confirmation/open",
        ],
        "display_note": "This is the current snapshot trade plan, not a standing order. Reconfirm after each scan.",
    }


def _entry_reference_price(direction: str, price: float, execution_zone: dict[str, Any]) -> float:
    lower = execution_zone.get("lower")
    upper = execution_zone.get("upper")
    if isinstance(lower, (int, float)) and isinstance(upper, (int, float)) and lower <= upper:
        return min(max(price, float(lower)), float(upper))
    return price


def _risk_reward_ratio(direction: str, reference: float, stop_loss: Any, take_profit: Any) -> float | None:
    if not isinstance(stop_loss, (int, float)) or not isinstance(take_profit, (int, float)):
        return None
    risk = abs(reference - float(stop_loss))
    reward = abs(float(take_profit) - reference)
    if risk <= 0:
        return None
    if direction == "long" and not (float(stop_loss) < reference < float(take_profit)):
        return None
    if direction == "short" and not (float(take_profit) < reference < float(stop_loss)):
        return None
    return reward / risk


def _plan_key(direction: str, reference: float, execution_zone: dict[str, Any], stop_loss: Any, take_profit: Any) -> str:
    values = [
        direction,
        _rounded(reference),
        _rounded(execution_zone.get("lower")),
        _rounded(execution_zone.get("upper")),
        _rounded(stop_loss),
        _rounded(take_profit),
    ]
    return ":".join(values)


def _rounded(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.4f}"
    return "none"


def _score_breakdown(
    *,
    score: float,
    min_score: float,
    modules: list[dict[str, Any]],
    execution_ready: bool,
    execution_zone: dict[str, Any],
    required_missing: list[str],
    weighted_score: float | None = None,
    signal_weights: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    structure_points = sum(float(module.get("points") or 0.0) for module in modules)
    execution_checks = [
        not required_missing,
        score >= min_score,
        bool(execution_zone.get("valid")),
        bool(execution_zone.get("inside")),
        execution_ready,
    ]
    execution_score = round(sum(1 for item in execution_checks if item) / len(execution_checks) * 100, 1)
    return {
        "structure_score": round(structure_points, 1),
        "weighted_score": round(weighted_score if weighted_score is not None else score, 1),
        "weight_mode": "governed" if signal_weights else "baseline",
        "weight_multipliers": _weight_multipliers(modules, signal_weights or {}),
        "execution_score": execution_score,
        "min_score": min_score,
        "execution_ready": execution_ready,
        "checks": {
            "required_data_ready": not required_missing,
            "score_above_minimum": score >= min_score,
            "execution_zone_valid": bool(execution_zone.get("valid")),
            "price_inside_execution_zone": bool(execution_zone.get("inside")),
            "gate_ready": execution_ready,
        },
    }


def _weighted_score(modules: list[dict[str, Any]], signal_weights: dict[str, dict[str, Any]]) -> float:
    total = 0.0
    for module in modules:
        points = float(module.get("points") or 0.0)
        multiplier = float((signal_weights.get(str(module.get("name"))) or {}).get("multiplier") or 1.0)
        total += points * multiplier
    return round(total, 1)


def _weight_multipliers(
    modules: list[dict[str, Any]],
    signal_weights: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for module in modules:
        name = str(module.get("name"))
        weight = signal_weights.get(name) or {}
        rows.append(
            {
                "signal_name": name,
                "base_points": float(module.get("points") or 0.0),
                "multiplier": float(weight.get("multiplier") or 1.0),
                "weighted_points": round(float(module.get("points") or 0.0) * float(weight.get("multiplier") or 1.0), 2),
                "status": weight.get("status") or "baseline",
                "sample_count": weight.get("sample_count") or 0,
            }
        )
    return rows


def _execution_gate(
    asset_tier: str,
    direction: str,
    price: float,
    short_state: TimeframeState | None,
    mid_state: TimeframeState | None,
    risk_payload: dict[str, Any],
) -> dict[str, Any]:
    warnings: list[str] = []
    reasons: list[str] = []
    entry_zone = _entry_zone(asset_tier, price, short_state)
    execution_zone = _execution_zone(
        direction=direction,
        price=price,
        cost_zone=entry_zone,
        stop_loss=risk_payload.get("stop_loss"),
        take_profit=risk_payload.get("take_profit"),
        min_r=_min_execution_r(asset_tier),
    )
    ready = True

    if short_state is None or short_state.bias != direction:
        ready = False
        warnings.append("短期触发未与长期方向一致，降级为观察候选。")
    if mid_state is None or mid_state.bias != direction:
        ready = False
        warnings.append("中期结构未确认，降级为观察候选。")
    if not entry_zone["inside"]:
        ready = False
        warnings.append("当前价格不在短期成本带入场区内，避免追价。")
    if not execution_zone["valid"]:
        ready = False
        warnings.append("成本带与止盈/止损结构冲突，没有满足最低 R 倍数的可执行入场区。")
    elif not execution_zone["inside"]:
        ready = False
        warnings.append("当前价格不在可执行入场区内，等待更好的锁价位置。")
    if risk_payload.get("stop_source") == "risk_template_stop":
        ready = False
        warnings.append("止损没有暗流失效位，只能固定风控兜底，暂不视为可开仓。")
    if risk_payload.get("target_source") == "risk_reward_template":
        ready = False
        warnings.append("止盈没有暗流目标位，只能固定盈亏比兜底，暂不视为可开仓。")
    if ready:
        reasons.append("短中长期方向一致，价格在短期成本带入场区，止盈止损均来自暗流指标。")

    return {
        "ready": ready,
        "entry_zone": entry_zone,
        "execution_zone": execution_zone,
        "reasons": reasons,
        "warnings": warnings,
    }


def _entry_zone(
    asset_tier: str,
    price: float,
    short_state: TimeframeState | None,
) -> dict[str, Any]:
    anchor = short_state.avg_price if short_state and short_state.avg_price else price
    limit_pct = _distance_limit(asset_tier, "short")
    lower = anchor * (1 - limit_pct)
    upper = anchor * (1 + limit_pct)
    distance_pct = abs(price - anchor) / anchor if anchor else None
    return {
        "anchor": anchor,
        "lower": lower,
        "upper": upper,
        "limit_pct": limit_pct,
        "distance_pct": distance_pct,
        "inside": lower <= price <= upper,
        "source": "short_smart_money_cost",
    }


def _execution_zone(
    direction: str,
    price: float,
    cost_zone: dict[str, Any],
    stop_loss: Any,
    take_profit: Any,
    min_r: float,
) -> dict[str, Any]:
    lower = float(cost_zone["lower"])
    upper = float(cost_zone["upper"])
    valid = isinstance(stop_loss, (int, float)) and isinstance(take_profit, (int, float))
    if not valid:
        return _empty_execution_zone(cost_zone, min_r)

    if direction == "long":
        # Entry must be above stop, below target, and cheap enough to preserve minimum R.
        max_entry_by_r = (float(take_profit) + min_r * float(stop_loss)) / (1 + min_r)
        executable_lower = max(lower, float(stop_loss))
        executable_upper = min(upper, float(take_profit), max_entry_by_r)
    else:
        # Entry must be below stop, above target, and high enough to preserve minimum R.
        min_entry_by_r = (float(take_profit) + min_r * float(stop_loss)) / (1 + min_r)
        executable_lower = max(lower, float(take_profit), min_entry_by_r)
        executable_upper = min(upper, float(stop_loss))

    is_valid = executable_lower <= executable_upper
    return {
        "lower": executable_lower if is_valid else None,
        "upper": executable_upper if is_valid else None,
        "source": "cost_band_intersect_risk",
        "min_r": min_r,
        "inside": is_valid and executable_lower <= price <= executable_upper,
        "valid": is_valid,
        "cost_lower": lower,
        "cost_upper": upper,
    }


def _empty_execution_zone(cost_zone: dict[str, Any], min_r: float) -> dict[str, Any]:
    return {
        "lower": None,
        "upper": None,
        "source": "missing_risk_levels",
        "min_r": min_r,
        "inside": False,
        "valid": False,
        "cost_lower": cost_zone["lower"],
        "cost_upper": cost_zone["upper"],
    }


def _min_execution_r(asset_tier: str) -> float:
    return 1.15 if asset_tier == "high_volatility" else 1.25


def _opportunity_stage(
    score: float,
    min_score: float,
    required_missing: list[str],
    risk_payload: dict[str, Any],
) -> dict[str, str]:
    gate = risk_payload.get("execution_gate") or {}
    execution_zone = risk_payload.get("execution_zone") or {}
    if required_missing:
        return {
            "key": "data_incomplete",
            "label": "数据未满",
            "reason": "核心评分指标仍有缺失，不能进入纸上开仓。",
        }
    if score < min_score:
        return {
            "key": "structure_watch",
            "label": "结构观察",
            "reason": "评分未达到当前币种最低开仓线。",
        }
    if gate.get("ready"):
        return {
            "key": "paper_ready",
            "label": "可纸上开仓",
            "reason": "方向、入场区、止盈止损和最低 R 倍数均通过。",
        }
    if execution_zone.get("valid") and not execution_zone.get("inside"):
        return {
            "key": "waiting_entry",
            "label": "等待入场",
            "reason": "结构满足，但当前价格还没有进入可执行入场区。",
        }
    if risk_payload.get("stop_source") == "risk_template_stop" or risk_payload.get("target_source") == "risk_reward_template":
        return {
            "key": "risk_incomplete",
            "label": "风控不足",
            "reason": "止盈或止损仍依赖固定兜底，暂不进入纸上开仓。",
        }
    return {
        "key": "structure_watch",
        "label": "结构观察",
        "reason": "信号结构仍有冲突，等待下一次扫描确认。",
    }


def _add_module(
    modules: list[dict[str, Any]],
    name: str,
    points: float,
    detail: str,
    status: str,
) -> float:
    modules.append({"name": name, "points": points, "detail": detail, "status": status})
    return points


def _score_liquidity_module(
    modules: list[dict[str, Any]],
    snapshots: dict[str, SignalSnapshot],
    reason: dict[str, Any],
) -> float:
    present = [key for key in ("liq_heatmap", "liquidation_fuel", "liquidity_sweep") if key in snapshots]
    if len(present) >= 2:
        reason["rules"].append("liquidity_context_present")
        reason["explanation"].append("清算/流动性模块有数据，可辅助判断目标位和扫损风险。")
        return _add_module(modules, "清算流动性", 10, f"已接入 {len(present)} 个流动性指标。", "ok")
    reason["warnings"].append("清算/流动性指标不足。")
    return _add_module(modules, "清算流动性", 0, "清算/流动性指标不足，不能确认磁吸目标。", "missing")


def _score_orderflow_module(
    modules: list[dict[str, Any]],
    snapshots: dict[str, SignalSnapshot],
    reason: dict[str, Any],
) -> float:
    present = [key for key in ("cross_exchange_resonance", "imbalance") if key in snapshots]
    if len(present) == 2:
        reason["rules"].append("orderflow_present")
        reason["explanation"].append("大单共振和多空失衡数据存在，可辅助确认触发质量。")
        return _add_module(modules, "订单流确认", 10, "大单共振和多空失衡均有数据。", "ok")
    reason["warnings"].append("订单流确认不足。")
    return _add_module(modules, "订单流确认", 0, "缺少大单共振或多空失衡数据。", "missing")


def _score_exhaustion_module(
    modules: list[dict[str, Any]],
    snapshots: dict[str, SignalSnapshot],
    reason: dict[str, Any],
) -> float:
    if "trend_exhaustion" in snapshots:
        reason["rules"].append("exhaustion_present")
        return _add_module(modules, "趋势耗竭", 5, "趋势耗竭指标存在，可用于过滤追单风险。", "ok")
    reason["warnings"].append("缺少趋势耗竭过滤。")
    return _add_module(modules, "趋势耗竭", 0, "缺少趋势耗竭指标。", "missing")


def _score_data_quality_module(
    modules: list[dict[str, Any]],
    snapshots: dict[str, SignalSnapshot],
    reason: dict[str, Any],
) -> float:
    present_ratio = len(snapshots) / len(CORE_INDICATORS)
    if present_ratio >= 0.75:
        return _add_module(modules, "全量研究覆盖", 5, "全量研究指标覆盖率较高。", "ok")
    reason["warnings"].append(f"全量研究覆盖率偏低：{present_ratio:.0%}。")
    return _add_module(
        modules,
        "全量研究覆盖",
        0,
        f"全量研究覆盖率 {present_ratio:.0%}，可纸上验证，但不应直接视为高置信实盘信号。",
        "warn",
    )


def _missing_required_indicators(snapshots: dict[str, SignalSnapshot]) -> list[str]:
    return [indicator for indicator in REQUIRED_SCORING_INDICATORS if indicator not in snapshots]


def _snapshot_is_fresh(snapshot: SignalSnapshot) -> bool:
    return not _snapshot_staleness(snapshot)[0]


def _snapshot_staleness(snapshot: SignalSnapshot) -> tuple[bool, float]:
    collected_at = _as_aware(snapshot.collected_at)
    now = datetime.now(timezone.utc)
    age_minutes = max((now - collected_at).total_seconds() / 60, 0)
    return _is_stale(collected_at, now, snapshot.timeframe), age_minutes


def _cn_direction(direction: str) -> str:
    if direction == "long":
        return "多"
    if direction == "short":
        return "空"
    return "中性"
