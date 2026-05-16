from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DarkflowInteraction, PriceSnapshot, ShadowPaperTrade, TradeCandidate, utc_now
from app.services.darkflow_decision_cards import (
    PROMOTION_BLOCKER_ANTI_REPAINT_FAILED,
    PROMOTION_BLOCKER_ANTI_REPAINT_MISSING,
    PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING,
    PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED,
    PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING,
    decision_card_from_interaction,
)
DARKFLOW_V2_SHADOW_STRATEGY_NAME = "darkflow_v2_trade_candidate_shadow_forward_v1"
DEFAULT_PROMOTION_LIMIT = 500
DEFAULT_SHADOW_FORWARD_LIMIT = 100
DEFAULT_MAX_CANDIDATE_AGE_HOURS = 72.0
DEFAULT_ENTRY_TOLERANCE_PCT = 0.025
SHADOW_FEE_RATE = 0.0004
SHADOW_SLIPPAGE_RATE_BY_TIER = {
    "core": 0.0002,
    "mainstream": 0.00035,
    "high_volatility": 0.0007,
}
SHADOW_FORWARD_MIN_CLOSED_TRADES = 10
SHADOW_FORWARD_MIN_WIN_RATE = 0.52
SHADOW_FORWARD_MIN_PROFIT_FACTOR = 1.15
SHADOW_FORWARD_MAX_DRAWDOWN = 0.12


async def audit_darkflow_trade_candidates(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_PROMOTION_LIMIT,
    include_blocked: bool = False,
) -> dict[str, Any]:
    rows = await _candidate_rows(session, limit=limit, include_blocked=include_blocked)
    passed = 0
    failed = 0
    skipped: list[dict[str, Any]] = []
    audited: list[dict[str, Any]] = []
    now = utc_now()
    for candidate in rows:
        if candidate.status != "shadow_candidate" and not include_blocked:
            skipped.append({"candidate_key": candidate.candidate_key, "reason": "not_shadow_candidate"})
            continue
        source = await _source_interaction(session, candidate)
        result = _anti_repaint_audit(candidate, source)
        candidate.anti_repaint_status = "passed" if result["passed"] else "failed"
        candidate.updated_at = now
        _apply_lifecycle(candidate)
        if result["passed"]:
            passed += 1
        else:
            failed += 1
        audited.append(
            {
                "candidate_key": candidate.candidate_key,
                "anti_repaint_status": candidate.anti_repaint_status,
                "promotion_status": candidate.promotion_status,
                "checks": result["checks"],
                "failures": result["failures"],
            }
        )
    if audited:
        await session.commit()
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "requested_limit": max(1, int(limit)),
        "audited": len(audited),
        "passed": passed,
        "failed": failed,
        "skipped": skipped[:50],
        "rows": audited[:100],
        "policy": _policy(),
    }


async def open_darkflow_shadow_forward_samples(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_SHADOW_FORWARD_LIMIT,
    max_candidate_age_hours: float = DEFAULT_MAX_CANDIDATE_AGE_HOURS,
    entry_tolerance_pct: float = DEFAULT_ENTRY_TOLERANCE_PCT,
) -> dict[str, Any]:
    rows = await _shadow_ready_candidate_rows(session, limit=limit)
    opened: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    now = utc_now()
    for candidate in rows:
        stats = await _shadow_stats(session, candidate.candidate_key)
        if _update_shadow_lifecycle(candidate, stats, now=now):
            updated.append(
                {
                    "candidate_key": candidate.candidate_key,
                    "shadow_status": candidate.shadow_status,
                    "promotion_status": candidate.promotion_status,
                    "closed_trades": stats["closed_trades"],
                    "open_trades": stats["open_trades"],
                }
            )
        if candidate.shadow_status in {"passed", "failed"}:
            skipped.append({"candidate_key": candidate.candidate_key, "reason": f"shadow_{candidate.shadow_status}"})
            continue
        if stats["open_trades"]:
            skipped.append({"candidate_key": candidate.candidate_key, "reason": "open_shadow_forward_exists"})
            continue
        plan_check = _candidate_plan_openable(
            candidate,
            now=now,
            max_candidate_age_hours=max_candidate_age_hours,
        )
        if plan_check is not None:
            skipped.append({"candidate_key": candidate.candidate_key, "reason": plan_check})
            continue
        price = await _latest_price(session, candidate.symbol)
        if price is None or price <= 0:
            skipped.append({"candidate_key": candidate.candidate_key, "symbol": candidate.symbol, "reason": "missing_latest_price"})
            continue
        entry_plan_state = _candidate_entry_plan_state(
            candidate,
            mark_price=price,
            now=now,
            entry_tolerance_pct=entry_tolerance_pct,
        )
        if entry_plan_state["state"] != "triggered":
            skipped.append(
                {
                    "candidate_key": candidate.candidate_key,
                    "symbol": candidate.symbol,
                    "reason": f"entry_plan_{entry_plan_state['state']}",
                    "mark_price": price,
                    "planned_entry": candidate.entry_price,
                    "entry_plan_state": entry_plan_state,
                }
            )
            continue
        trade = _shadow_trade_from_candidate(
            candidate,
            mark_price=price,
            now=now,
            entry_tolerance_pct=entry_tolerance_pct,
            entry_plan_state=entry_plan_state,
        )
        if trade is None:
            skipped.append({"candidate_key": candidate.candidate_key, "reason": "invalid_shadow_trade_shape"})
            continue
        session.add(trade)
        candidate.shadow_status = "collecting"
        candidate.updated_at = now
        _apply_lifecycle(candidate)
        opened.append(
            {
                "candidate_key": candidate.candidate_key,
                "symbol": candidate.symbol,
                "direction": candidate.direction,
                "entry_price": trade.entry_price,
                "stop_loss": trade.stop_loss,
                "take_profit": trade.take_profit,
                "entry_plan_state": entry_plan_state,
            }
        )
    if opened or updated:
        await session.commit()
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "requested_limit": max(1, int(limit)),
        "opened": opened,
        "updated": updated[:100],
        "skipped": skipped[:100],
        "thresholds": {
            "max_candidate_age_hours": float(max_candidate_age_hours),
            "entry_tolerance_pct": float(entry_tolerance_pct),
        },
        "policy": _policy(),
    }


async def refresh_darkflow_candidate_promotion(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_PROMOTION_LIMIT,
    shadow_limit: int = DEFAULT_SHADOW_FORWARD_LIMIT,
    max_candidate_age_hours: float = DEFAULT_MAX_CANDIDATE_AGE_HOURS,
    entry_tolerance_pct: float = DEFAULT_ENTRY_TOLERANCE_PCT,
) -> dict[str, Any]:
    audit = await audit_darkflow_trade_candidates(session, limit=limit)
    shadow = await open_darkflow_shadow_forward_samples(
        session,
        limit=shadow_limit,
        max_candidate_age_hours=max_candidate_age_hours,
        entry_tolerance_pct=entry_tolerance_pct,
    )
    summary = await darkflow_candidate_promotion_report(session, limit=limit)
    return {"strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME, "audit": audit, "shadow_forward": shadow, "summary": summary, "policy": _policy()}


async def darkflow_candidate_promotion_report(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_PROMOTION_LIMIT,
) -> dict[str, Any]:
    rows = await _candidate_rows(session, limit=limit, include_blocked=True)
    counts: dict[str, int] = {}
    anti_counts: dict[str, int] = {}
    shadow_counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    for candidate in rows:
        counts[candidate.promotion_status] = counts.get(candidate.promotion_status, 0) + 1
        anti_counts[candidate.anti_repaint_status] = anti_counts.get(candidate.anti_repaint_status, 0) + 1
        shadow_counts[candidate.shadow_status] = shadow_counts.get(candidate.shadow_status, 0) + 1
        if len(samples) < 25:
            stats = await _shadow_stats(session, candidate.candidate_key)
            samples.append(
                {
                    "candidate_key": candidate.candidate_key,
                    "symbol": candidate.symbol,
                    "direction": candidate.direction,
                    "status": candidate.status,
                    "promotion_status": candidate.promotion_status,
                    "anti_repaint_status": candidate.anti_repaint_status,
                    "shadow_status": candidate.shadow_status,
                    "promotion_blockers": candidate.promotion_blockers,
                    "shadow_stats": stats,
                }
            )
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "candidate_count": len(rows),
        "promotion_status_counts": counts,
        "anti_repaint_status_counts": anti_counts,
        "shadow_status_counts": shadow_counts,
        "samples": samples,
        "criteria": _shadow_criteria(),
        "policy": _policy(),
    }


async def darkflow_entry_plan_state_report(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_PROMOTION_LIMIT,
    entry_tolerance_pct: float = DEFAULT_ENTRY_TOLERANCE_PCT,
) -> dict[str, Any]:
    rows = await _candidate_rows(session, limit=limit, include_blocked=True)
    now = utc_now()
    prices = await _latest_prices(session, sorted({candidate.symbol for candidate in rows}))
    counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    missing_price_count = 0
    samples: list[dict[str, Any]] = []
    for candidate in rows:
        price = prices.get(candidate.symbol)
        if price is None or price <= 0:
            state = _missing_price_entry_plan_state(candidate, now=now)
            missing_price_count += 1
        else:
            state = _candidate_entry_plan_state(
                candidate,
                mark_price=price,
                now=now,
                entry_tolerance_pct=entry_tolerance_pct,
            )
        state_key = str(state.get("state") or "unknown")
        reason_key = str(state.get("reason") or "unknown")
        counts[state_key] = counts.get(state_key, 0) + 1
        reason_counts[reason_key] = reason_counts.get(reason_key, 0) + 1
        if len(samples) < 25:
            samples.append(
                {
                    "candidate_key": candidate.candidate_key,
                    "symbol": candidate.symbol,
                    "direction": candidate.direction,
                    "status": candidate.status,
                    "promotion_status": candidate.promotion_status,
                    "anti_repaint_status": candidate.anti_repaint_status,
                    "shadow_status": candidate.shadow_status,
                    "entry_price": candidate.entry_price,
                    "stop_price": candidate.stop_price,
                    "target_price": candidate.target_price,
                    "quality_score": candidate.quality_score,
                    "entry_plan_state": state,
                }
            )
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "requested_limit": max(1, int(limit)),
        "candidate_count": len(rows),
        "generated_at": _iso(now),
        "state_counts": counts,
        "reason_counts": reason_counts,
        "missing_price_count": missing_price_count,
        "samples": samples,
        "thresholds": {"entry_tolerance_pct": float(entry_tolerance_pct)},
        "policy": _policy() | {
            "opens_paper_trades": False,
            "opens_live_orders": False,
            "mutates_candidate_state": False,
            "report_only": True,
        },
    }


async def _candidate_rows(session: AsyncSession, *, limit: int, include_blocked: bool) -> list[TradeCandidate]:
    query = select(TradeCandidate).where(TradeCandidate.lineage == "core_darkflow_v2")
    if not include_blocked:
        query = query.where(TradeCandidate.status == "shadow_candidate")
    rows = await session.scalars(
        query.order_by(TradeCandidate.setup_time.desc(), TradeCandidate.updated_at.desc(), TradeCandidate.id.desc()).limit(max(1, int(limit)))
    )
    return list(rows.all())


async def _shadow_ready_candidate_rows(session: AsyncSession, *, limit: int) -> list[TradeCandidate]:
    rows = await session.scalars(
        select(TradeCandidate)
        .where(
            TradeCandidate.lineage == "core_darkflow_v2",
            TradeCandidate.status == "shadow_candidate",
            TradeCandidate.anti_repaint_status == "passed",
            TradeCandidate.shadow_status.in_(["not_started", "collecting"]),
        )
        .order_by(TradeCandidate.setup_time.desc(), TradeCandidate.updated_at.desc(), TradeCandidate.id.desc())
        .limit(max(1, int(limit)))
    )
    return list(rows.all())


async def _source_interaction(session: AsyncSession, candidate: TradeCandidate) -> DarkflowInteraction | None:
    if candidate.source_interaction_id:
        row = await session.get(DarkflowInteraction, candidate.source_interaction_id)
        if row is not None:
            return row
    payload = candidate.decision_payload or {}
    source_id = payload.get("source_interaction_id")
    if isinstance(source_id, str) and source_id:
        return await session.get(DarkflowInteraction, source_id)
    return None


def _anti_repaint_audit(candidate: TradeCandidate, source: DarkflowInteraction | None) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    failures: list[str] = []
    if source is None:
        return {"passed": False, "checks": {"source_interaction_exists": False}, "failures": ["source_interaction_missing"]}
    card = decision_card_from_interaction(source)
    checks["source_interaction_exists"] = True
    checks["decision_card_rebuilds"] = card is not None
    if card is None:
        failures.append("decision_card_rebuild_failed")
        return {"passed": False, "checks": checks, "failures": failures}
    checks["candidate_key_stable"] = card["card_id"] == candidate.candidate_key
    checks["source_id_stable"] = str(card.get("source_interaction_id") or "") == str(candidate.source_interaction_id or "")
    entry_plan = card["entry_plan"]
    target = entry_plan["take_profit_levels"][0]
    checks["entry_stable"] = _close(entry_plan["planned_entry"], candidate.entry_price)
    checks["stop_stable"] = _close(entry_plan["planned_stop"], candidate.stop_price)
    checks["target_stable"] = _close(target["price"], candidate.target_price)
    checks["direction_stable"] = str(card["direction"]) == candidate.direction
    checks["strategy_stable"] = str(card["strategy_id"]) == candidate.strategy_id
    checks["research_only"] = bool((card.get("context") or {}).get("research_only")) is True
    checks["no_execution_flags"] = card["risk_gate"].get("paper_eligible") is False and card["risk_gate"].get("live_eligible") is False
    for key, passed in checks.items():
        if not passed:
            failures.append(key)
    return {"passed": not failures, "checks": checks, "failures": failures}


async def _latest_price(session: AsyncSession, symbol: str) -> float | None:
    row = await session.scalar(
        select(PriceSnapshot.price).where(PriceSnapshot.symbol == symbol).order_by(PriceSnapshot.created_at.desc()).limit(1)
    )
    return float(row) if isinstance(row, (int, float)) else None


async def _latest_prices(session: AsyncSession, symbols: list[str]) -> dict[str, float]:
    prices: dict[str, float] = {}
    for symbol in symbols:
        price = await _latest_price(session, symbol)
        if price is not None and price > 0:
            prices[symbol] = price
    return prices


async def _shadow_stats(session: AsyncSession, candidate_key: str) -> dict[str, Any]:
    rows = await session.scalars(
        select(ShadowPaperTrade)
        .where(
            ShadowPaperTrade.strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME,
            ShadowPaperTrade.candidate_key == candidate_key,
        )
        .order_by(ShadowPaperTrade.opened_at)
    )
    trades = list(rows.all())
    closed = [item for item in trades if item.status == "closed" and isinstance(item.pnl, (int, float))]
    wins = [float(item.pnl) for item in closed if float(item.pnl or 0.0) > 0]
    losses = [float(item.pnl) for item in closed if float(item.pnl or 0.0) < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    returns = [float(item.pnl or 0.0) for item in closed]
    return {
        "open_trades": sum(1 for item in trades if item.status == "open"),
        "closed_trades": len(closed),
        "total_trades": len(trades),
        "win_rate": len(wins) / len(closed) if closed else None,
        "avg_pnl": mean(returns) if returns else None,
        "profit_factor": gross_win / gross_loss if gross_loss else (999.0 if gross_win else None),
        "max_drawdown": _max_drawdown(returns),
    }


def _update_shadow_lifecycle(candidate: TradeCandidate, stats: dict[str, Any], *, now: datetime) -> bool:
    previous = (candidate.shadow_status, candidate.promotion_status, tuple(candidate.promotion_blockers or []))
    if stats["closed_trades"] >= SHADOW_FORWARD_MIN_CLOSED_TRADES:
        candidate.shadow_status = "passed" if _shadow_stats_pass(stats) else "failed"
    elif stats["open_trades"] or stats["total_trades"]:
        candidate.shadow_status = "collecting"
    else:
        candidate.shadow_status = "not_started"
    candidate.updated_at = now
    _apply_lifecycle(candidate)
    return previous != (candidate.shadow_status, candidate.promotion_status, tuple(candidate.promotion_blockers or []))


def _shadow_stats_pass(stats: dict[str, Any]) -> bool:
    win_rate = stats.get("win_rate")
    profit_factor = stats.get("profit_factor")
    max_drawdown = float(stats.get("max_drawdown") or 0.0)
    return (
        isinstance(win_rate, (int, float))
        and float(win_rate) >= SHADOW_FORWARD_MIN_WIN_RATE
        and isinstance(profit_factor, (int, float))
        and float(profit_factor) >= SHADOW_FORWARD_MIN_PROFIT_FACTOR
        and max_drawdown <= SHADOW_FORWARD_MAX_DRAWDOWN
    )


def _candidate_plan_openable(candidate: TradeCandidate, *, now: datetime, max_candidate_age_hours: float) -> str | None:
    if candidate.direction not in {"long", "short"}:
        return "unsupported_direction"
    if candidate.entry_price <= 0 or candidate.stop_price <= 0 or candidate.target_price <= 0:
        return "invalid_plan_prices"
    if candidate.direction == "long" and not (candidate.stop_price < candidate.entry_price < candidate.target_price):
        return "invalid_long_reward_shape"
    if candidate.direction == "short" and not (candidate.target_price < candidate.entry_price < candidate.stop_price):
        return "invalid_short_reward_shape"
    setup_time = _aware(candidate.setup_time) if candidate.setup_time else None
    if setup_time and max_candidate_age_hours > 0:
        if now - setup_time > timedelta(hours=float(max_candidate_age_hours)):
            return "stale_candidate"
    return None


def _within_entry_tolerance(price: float, planned_entry: float, *, entry_tolerance_pct: float) -> bool:
    if planned_entry <= 0:
        return False
    return abs(price - planned_entry) / planned_entry <= max(0.0, float(entry_tolerance_pct))


def _candidate_entry_plan_state(
    candidate: TradeCandidate,
    *,
    mark_price: float,
    now: datetime,
    entry_tolerance_pct: float,
) -> dict[str, Any]:
    plan = _entry_plan(candidate)
    planned_entry = _float(plan.get("planned_entry")) or float(candidate.entry_price)
    planned_stop = _float(plan.get("planned_stop")) or float(candidate.stop_price)
    target = _target_price_from_plan(plan) or float(candidate.target_price)
    invalidation = _float(plan.get("invalidation_price")) or planned_stop
    lower, upper, range_source = _entry_range(
        plan,
        direction=candidate.direction,
        planned_entry=planned_entry,
        planned_stop=planned_stop,
        target_price=target,
        entry_tolerance_pct=entry_tolerance_pct,
    )
    valid_until = _parse_iso_datetime(plan.get("valid_until"))
    state = {
        "state": "waiting",
        "reason": "awaiting_frozen_entry_range",
        "plan_type": str(plan.get("plan_type") or "legacy_entry_plan"),
        "evaluated_at": _iso(now),
        "mark_price": float(mark_price),
        "planned_entry": planned_entry,
        "planned_stop": planned_stop,
        "target_price": target,
        "invalidation_price": invalidation,
        "entry_range": {"lower": lower, "upper": upper, "source": range_source},
        "valid_until": _iso(valid_until),
    }
    if valid_until is not None and _aware(now) > valid_until:
        state.update({"state": "expired", "reason": "valid_until_passed"})
        return state
    shape_error = _entry_plan_shape_error(
        candidate.direction,
        entry=planned_entry,
        stop=planned_stop,
        target=target,
        lower=lower,
        upper=upper,
    )
    if shape_error:
        state.update({"state": "invalid_shape", "reason": shape_error})
        return state
    if _price_invalidated(candidate.direction, mark_price=mark_price, invalidation=invalidation, stop=planned_stop):
        state.update({"state": "invalidated", "reason": "price_crosses_invalidation"})
        return state
    if lower <= mark_price <= upper:
        state.update({"state": "triggered", "reason": "mark_price_inside_frozen_entry_range"})
        return state
    if _entry_range_missed(candidate.direction, mark_price=mark_price, lower=lower, upper=upper):
        state.update({"state": "missed", "reason": "entry_range_missed"})
        return state
    return state


def _missing_price_entry_plan_state(candidate: TradeCandidate, *, now: datetime) -> dict[str, Any]:
    plan = _entry_plan(candidate)
    planned_entry = _float(plan.get("planned_entry")) or float(candidate.entry_price)
    planned_stop = _float(plan.get("planned_stop")) or float(candidate.stop_price)
    target = _target_price_from_plan(plan) or float(candidate.target_price)
    invalidation = _float(plan.get("invalidation_price")) or planned_stop
    lower, upper, range_source = _entry_range(
        plan,
        direction=candidate.direction,
        planned_entry=planned_entry,
        planned_stop=planned_stop,
        target_price=target,
        entry_tolerance_pct=DEFAULT_ENTRY_TOLERANCE_PCT,
    )
    valid_until = _parse_iso_datetime(plan.get("valid_until"))
    state = {
        "state": "missing_price",
        "reason": "missing_latest_price",
        "plan_type": str(plan.get("plan_type") or "legacy_entry_plan"),
        "evaluated_at": _iso(now),
        "mark_price": None,
        "planned_entry": planned_entry,
        "planned_stop": planned_stop,
        "target_price": target,
        "invalidation_price": invalidation,
        "entry_range": {"lower": lower, "upper": upper, "source": range_source},
        "valid_until": _iso(valid_until),
    }
    if valid_until is not None and _aware(now) > valid_until:
        state.update({"state": "expired", "reason": "valid_until_passed"})
    return state


def _entry_plan(candidate: TradeCandidate) -> dict[str, Any]:
    payload = candidate.decision_payload if isinstance(candidate.decision_payload, dict) else {}
    plan = payload.get("entry_plan") if isinstance(payload.get("entry_plan"), dict) else {}
    return dict(plan)


def _entry_range(
    plan: dict[str, Any],
    *,
    direction: str,
    planned_entry: float,
    planned_stop: float,
    target_price: float,
    entry_tolerance_pct: float,
) -> tuple[float, float, str]:
    raw = plan.get("entry_range") if isinstance(plan.get("entry_range"), dict) else {}
    lower = _float(raw.get("lower"))
    upper = _float(raw.get("upper"))
    if lower is not None and upper is not None and lower > 0 and upper > 0 and lower < upper:
        return float(lower), float(upper), str(raw.get("source") or "frozen_entry_range")
    tolerance = max(0.0, float(entry_tolerance_pct))
    width = planned_entry * tolerance
    lower = planned_entry - width
    upper = planned_entry + width
    epsilon = max(abs(planned_entry) * 1e-9, 1e-9)
    if direction == "long":
        lower = max(lower, planned_stop + epsilon)
        upper = min(upper, target_price - epsilon)
    elif direction == "short":
        lower = max(lower, target_price + epsilon)
        upper = min(upper, planned_stop - epsilon)
    return lower, upper, "runtime_entry_tolerance_fallback"


def _entry_plan_shape_error(
    direction: str,
    *,
    entry: float,
    stop: float,
    target: float,
    lower: float,
    upper: float,
) -> str | None:
    epsilon = max(abs(entry) * 1e-8, 1e-8)
    if direction not in {"long", "short"}:
        return "unsupported_direction"
    if min(entry, stop, target, lower, upper) <= 0 or lower >= upper:
        return "invalid_plan_prices"
    if direction == "long" and not (
        stop < lower
        and lower - epsilon <= entry <= upper + epsilon
        and upper < target
    ):
        return "invalid_long_frozen_entry_range"
    if direction == "short" and not (
        target < lower
        and lower - epsilon <= entry <= upper + epsilon
        and upper < stop
    ):
        return "invalid_short_frozen_entry_range"
    return None


def _price_invalidated(direction: str, *, mark_price: float, invalidation: float, stop: float) -> bool:
    boundary = invalidation if invalidation > 0 else stop
    if direction == "long":
        return mark_price <= boundary or mark_price <= stop
    if direction == "short":
        return mark_price >= boundary or mark_price >= stop
    return True


def _entry_range_missed(direction: str, *, mark_price: float, lower: float, upper: float) -> bool:
    if direction == "long":
        return mark_price > upper
    if direction == "short":
        return mark_price < lower
    return False


def _target_price_from_plan(plan: dict[str, Any]) -> float | None:
    levels = plan.get("take_profit_levels")
    if isinstance(levels, list) and levels:
        first = levels[0]
        if isinstance(first, dict):
            return _float(first.get("price"))
    return None


def _shadow_trade_from_candidate(
    candidate: TradeCandidate,
    *,
    mark_price: float,
    now: datetime,
    entry_tolerance_pct: float,
    entry_plan_state: dict[str, Any] | None = None,
) -> ShadowPaperTrade | None:
    asset_tier = _asset_tier(candidate.symbol)
    entry_price = _execution_price(candidate.direction, mark_price, side="entry", asset_tier=asset_tier)
    if candidate.direction == "long" and not (candidate.stop_price < entry_price < candidate.target_price):
        return None
    if candidate.direction == "short" and not (candidate.target_price < entry_price < candidate.stop_price):
        return None
    signal_key = _shadow_forward_signal_key(candidate, opened_at=now)
    return ShadowPaperTrade(
        strategy_name=DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        candidate_type="trade_candidate",
        candidate_key=candidate.candidate_key,
        signal_key=signal_key,
        source_experiment_run_id=None,
        symbol=candidate.symbol,
        timeframe=candidate.timeframe,
        direction=candidate.direction,
        entry_price=entry_price,
        stop_loss=candidate.stop_price,
        take_profit=candidate.target_price,
        position_size=1.0,
        status="open",
        mfe=0.0,
        mae=0.0,
        opened_at=now,
        context={
            "research_only": True,
            "shadow_forward": True,
            "opens_paper_trades": False,
            "opens_live_orders": False,
            "trade_candidate_id": candidate.id,
            "source_interaction_id": candidate.source_interaction_id,
            "lineage": candidate.lineage,
            "mark_price_at_signal": mark_price,
            "planned_entry_price": candidate.entry_price,
            "entry_tolerance_pct": entry_tolerance_pct,
            "entry_plan_state": entry_plan_state or {},
            "execution_model": _execution_model(asset_tier),
            "candidate_snapshot": _candidate_snapshot(candidate),
        },
    )


def _apply_lifecycle(candidate: TradeCandidate) -> None:
    blockers = set(str(item) for item in (candidate.promotion_blockers or []))
    blockers.discard("persistent_trade_candidate_table_missing")
    if candidate.anti_repaint_status == "passed":
        blockers.discard(PROMOTION_BLOCKER_ANTI_REPAINT_MISSING)
        blockers.discard(PROMOTION_BLOCKER_ANTI_REPAINT_FAILED)
    elif candidate.anti_repaint_status == "failed":
        blockers.discard(PROMOTION_BLOCKER_ANTI_REPAINT_MISSING)
        blockers.add(PROMOTION_BLOCKER_ANTI_REPAINT_FAILED)
    else:
        blockers.add(PROMOTION_BLOCKER_ANTI_REPAINT_MISSING)
    if candidate.shadow_status == "passed":
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED)
    elif candidate.shadow_status == "collecting":
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED)
        blockers.add(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING)
    elif candidate.shadow_status == "failed":
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING)
        blockers.add(PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED)
    else:
        blockers.add(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
    candidate.promotion_blockers = _ordered_blockers(blockers)
    candidate.paper_eligible = False
    candidate.live_eligible = False
    candidate.promotion_status = _promotion_status(candidate)


def _promotion_status(candidate: TradeCandidate) -> str:
    blockers = set(candidate.promotion_blockers or [])
    if candidate.status != "shadow_candidate":
        return "blocked"
    if candidate.anti_repaint_status == "failed" or PROMOTION_BLOCKER_ANTI_REPAINT_FAILED in blockers:
        return "anti_repaint_failed"
    if candidate.anti_repaint_status != "passed":
        return "anti_repaint_pending"
    if candidate.shadow_status == "failed" or PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED in blockers:
        return "shadow_forward_failed"
    if candidate.shadow_status == "collecting" or PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING in blockers:
        return "shadow_forward_collecting"
    if candidate.shadow_status != "passed":
        return "shadow_forward_pending"
    return "paper_review_ready"


def _ordered_blockers(blockers: set[str]) -> list[str]:
    ordered = [
        PROMOTION_BLOCKER_ANTI_REPAINT_MISSING,
        PROMOTION_BLOCKER_ANTI_REPAINT_FAILED,
        PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING,
        PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING,
        PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED,
    ]
    return [item for item in ordered if item in blockers] + sorted(blockers - set(ordered))


def _shadow_forward_signal_key(candidate: TradeCandidate, *, opened_at: datetime) -> str:
    slot = opened_at.replace(minute=0, second=0, microsecond=0).isoformat()
    raw = f"{DARKFLOW_V2_SHADOW_STRATEGY_NAME}:{candidate.candidate_key}:{candidate.symbol}:{slot}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _candidate_snapshot(candidate: TradeCandidate) -> dict[str, Any]:
    return {
        "candidate_key": candidate.candidate_key,
        "strategy_id": candidate.strategy_id,
        "strategy_name": candidate.strategy_name,
        "symbol": candidate.symbol,
        "timeframe": candidate.timeframe,
        "interval": candidate.interval,
        "direction": candidate.direction,
        "setup_type": candidate.setup_type,
        "market_state": candidate.market_state,
        "entry_price": candidate.entry_price,
        "stop_price": candidate.stop_price,
        "target_price": candidate.target_price,
        "rr_ratio": candidate.rr_ratio,
        "quality_score": candidate.quality_score,
        "supporting_signals": candidate.supporting_signals,
    }


def _asset_tier(symbol: str) -> str:
    coin = symbol.removesuffix("USDT")
    if coin in {"BTC", "ETH"}:
        return "core"
    if coin in {"SOL", "BNB", "LINK", "TON"}:
        return "mainstream"
    return "high_volatility"


def _execution_price(direction: str, price: float, *, side: str, asset_tier: str) -> float:
    slippage = _slippage_rate(asset_tier)
    if side == "entry":
        worse = 1 + slippage if direction == "long" else 1 - slippage
    else:
        worse = 1 - slippage if direction == "long" else 1 + slippage
    return price * worse


def _slippage_rate(asset_tier: str) -> float:
    return SHADOW_SLIPPAGE_RATE_BY_TIER.get(asset_tier, SHADOW_SLIPPAGE_RATE_BY_TIER["high_volatility"])


def _execution_model(asset_tier: str) -> dict[str, Any]:
    return {
        "fee_rate_per_side": SHADOW_FEE_RATE,
        "round_trip_fee_rate": SHADOW_FEE_RATE * 2,
        "slippage_rate": _slippage_rate(asset_tier),
        "entry_and_exit_use_worse_price": True,
        "mode": "conservative_darkflow_v2_shadow_forward",
    }


def _shadow_criteria() -> dict[str, Any]:
    return {
        "min_closed_trades": SHADOW_FORWARD_MIN_CLOSED_TRADES,
        "min_win_rate": SHADOW_FORWARD_MIN_WIN_RATE,
        "min_profit_factor": SHADOW_FORWARD_MIN_PROFIT_FACTOR,
        "max_drawdown": SHADOW_FORWARD_MAX_DRAWDOWN,
    }


def _policy() -> dict[str, Any]:
    return {
        "research_only": True,
        "opens_paper_trades": False,
        "opens_live_orders": False,
        "paper_eligible_after_task": False,
        "live_eligible_after_task": False,
        "isolated_strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "isolated_table": "shadow_paper_trades",
        "promotion_boundary": "paper_review_ready is still manual review only; no real paper/live orders are opened here.",
    }


def _close(left: Any, right: Any, *, rel_tol: float = 1e-9, abs_tol: float = 1e-8) -> bool:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    left_f = float(left)
    right_f = float(right)
    return abs(left_f - right_f) <= max(abs_tol, rel_tol * max(abs(left_f), abs(right_f), 1.0))


def _float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed == parsed else None
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if parsed == parsed else None
    return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value is not None else None


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        if peak:
            drawdown = max(drawdown, (peak - equity) / peak)
    return drawdown


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
