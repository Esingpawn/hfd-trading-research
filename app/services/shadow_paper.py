from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PriceSnapshot, ShadowPaperTrade
from app.services.feature_candidates import latest_feature_segment_candidate_screen
from app.services.paper import _exit_reason, _pnl
from app.services.risk import template_for_tier


SHADOW_STRATEGY_NAME = "shadow_feature_candidates_v1"
SHADOW_FEE_RATE = 0.0004
SHADOW_SLIPPAGE_RATE_BY_TIER = {
    "core": 0.0002,
    "mainstream": 0.00035,
    "high_volatility": 0.0007,
}
PROMOTION_MIN_CLOSED_TRADES = 30
PROMOTION_MIN_WIN_RATE = 0.52
PROMOTION_MIN_PROFIT_FACTOR = 1.25
PROMOTION_MAX_DRAWDOWN = 0.12


async def shadow_paper_scan(
    session: AsyncSession,
    *,
    candidate_limit: int = 20,
    include_watchlist: bool = True,
) -> dict[str, Any]:
    report = await latest_feature_segment_candidate_screen(session, horizon="30m")
    source_experiment_run_id = report.get("source_experiment_run_id")
    candidates = list(report.get("candidates") or [])
    candidate_type = "segment_candidate"
    if include_watchlist and not candidates:
        candidates = list(report.get("all_segments") or [])
        candidate_type = "observation_segment"
    opened: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in candidates[: max(0, candidate_limit)]:
        symbol = str(row.get("symbol") or "")
        direction = str(row.get("direction") or "")
        timeframe = str(row.get("timeframe") or "")
        candidate_key = str(row.get("segment_key") or row.get("feature_key") or "")
        if not symbol or direction not in {"long", "short"} or not candidate_key:
            skipped.append({"candidate_key": candidate_key, "reason": "incomplete_candidate"})
            continue
        existing_open = await _open_shadow_trade(session, candidate_key=candidate_key, symbol=symbol)
        if existing_open is not None:
            skipped.append({"candidate_key": candidate_key, "symbol": symbol, "reason": "open_shadow_trade_exists"})
            continue
        price = await _latest_price(session, symbol)
        if price is None or price <= 0:
            skipped.append({"candidate_key": candidate_key, "symbol": symbol, "reason": "missing_price"})
            continue
        asset_tier = _asset_tier(symbol)
        entry_price = _execution_price(direction, price, side="entry", asset_tier=asset_tier)
        levels = _shadow_levels(direction, entry_price, asset_tier)
        signal_key = _signal_key(candidate_key=candidate_key, symbol=symbol, direction=direction, price=price)
        trade = ShadowPaperTrade(
            strategy_name=SHADOW_STRATEGY_NAME,
            candidate_type=candidate_type,
            candidate_key=candidate_key,
            signal_key=signal_key,
            source_experiment_run_id=str(source_experiment_run_id) if source_experiment_run_id else None,
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            entry_price=entry_price,
            stop_loss=levels["stop_loss"],
            take_profit=levels["take_profit"],
            position_size=1.0,
            status="open",
            context={
                "research_only": True,
                "opens_paper_trades": False,
                "mark_price_at_signal": price,
                "execution_model": _execution_model(asset_tier),
                "candidate_snapshot": _candidate_context(row),
            },
        )
        session.add(trade)
        await session.flush()
        opened.append({"id": trade.id, "symbol": symbol, "candidate_key": candidate_key, "direction": direction})
    if opened:
        await session.commit()
    return {
        "strategy_name": SHADOW_STRATEGY_NAME,
        "source_experiment_run_id": source_experiment_run_id,
        "opened": opened,
        "skipped": skipped,
        "policy": _shadow_policy(),
    }


async def mark_shadow_paper_trades(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(
        select(ShadowPaperTrade).where(ShadowPaperTrade.status == "open").order_by(ShadowPaperTrade.opened_at)
    )
    closed: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    for trade in rows.scalars().all():
        price = await _latest_price(session, trade.symbol)
        if price is None:
            continue
        mark_pnl = _pnl(trade.direction, trade.entry_price, price)
        pnl = _net_pnl(trade, price, exit_side="mark")
        trade.mfe = max(trade.mfe, pnl)
        trade.mae = min(trade.mae, pnl)
        exit_reason = _exit_reason(trade, price)
        if exit_reason:
            exit_price = _execution_price(trade.direction, price, side="exit", asset_tier=_asset_tier(trade.symbol))
            pnl = _net_pnl(trade, exit_price, exit_side="executed")
            trade.status = "closed"
            trade.exit_price = exit_price
            trade.exit_reason = exit_reason
            trade.pnl = pnl
            stop_pct = abs(trade.entry_price - trade.stop_loss) / trade.entry_price
            trade.r_multiple = pnl / stop_pct if stop_pct else 0.0
            trade.closed_at = datetime.now(timezone.utc)
            trade.context = _merge_context(
                trade.context,
                {
                    "last_mark_price": price,
                    "exit_mark_price": price,
                    "exit_execution_price": exit_price,
                    "gross_pnl_before_cost": _pnl(trade.direction, trade.entry_price, exit_price),
                    "net_pnl_after_cost": pnl,
                    "total_fee_rate": SHADOW_FEE_RATE * 2,
                    "closed_by_shadow_mark": True,
                },
            )
            closed.append({"id": trade.id, "symbol": trade.symbol, "exit_reason": exit_reason, "pnl": pnl, "mark_pnl": mark_pnl})
        else:
            trade.context = _merge_context(trade.context, {"last_mark_price": price, "net_mark_pnl_after_cost": pnl})
            updated.append({"id": trade.id, "symbol": trade.symbol, "pnl": pnl, "mark_pnl": mark_pnl})
    if closed or updated:
        await session.commit()
    return {"closed": closed, "updated": updated, "policy": _shadow_policy()}


async def shadow_paper_trades(session: AsyncSession, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = await session.execute(select(ShadowPaperTrade).order_by(ShadowPaperTrade.opened_at.desc()).limit(limit))
    return [_trade_payload(item) for item in rows.scalars().all()]


async def shadow_paper_stats(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(select(ShadowPaperTrade))
    trades = rows.scalars().all()
    totals = _trade_stats(trades)
    by_candidate = _grouped_trade_stats(trades, key_func=_candidate_group_key)[:20]
    return {
        "strategy_name": SHADOW_STRATEGY_NAME,
        **totals,
        "by_candidate": by_candidate,
        "by_symbol": _grouped_trade_stats(trades, key_func=_symbol_group_key)[:20],
        "promotion": _promotion_report(by_candidate),
        "policy": _shadow_policy(),
    }


async def shadow_paper_promotion_report(session: AsyncSession) -> dict[str, Any]:
    stats = await shadow_paper_stats(session)
    return {
        "strategy_name": stats["strategy_name"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "promotion": stats["promotion"],
        "policy": stats["policy"],
    }


def _trade_stats(trades: list[ShadowPaperTrade]) -> dict[str, Any]:
    closed = [item for item in trades if item.status == "closed" and isinstance(item.pnl, (int, float))]
    wins = [float(item.pnl) for item in closed if item.pnl and item.pnl > 0]
    losses = [float(item.pnl) for item in closed if item.pnl and item.pnl < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    opened_times = [item.opened_at for item in trades if item.opened_at]
    closed_times = [item.closed_at for item in closed if item.closed_at]
    return {
        "open_trades": sum(1 for item in trades if item.status == "open"),
        "closed_trades": len(closed),
        "total_trades": len(trades),
        "win_rate": len(wins) / len(closed) if closed else None,
        "avg_pnl": mean([float(item.pnl) for item in closed]) if closed else None,
        "profit_factor": gross_win / gross_loss if gross_loss else (999.0 if gross_win else None),
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "max_drawdown": _max_drawdown([float(item.pnl or 0.0) for item in closed]),
        "execution_model": _execution_model("mixed"),
        "latest_opened_at": max(opened_times) if opened_times else None,
        "latest_closed_at": max(closed_times) if closed_times else None,
    }


def _grouped_trade_stats(
    trades: list[ShadowPaperTrade],
    *,
    key_func: Any,
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, ...], list[ShadowPaperTrade]] = {}
    for trade in trades:
        buckets.setdefault(key_func(trade), []).append(trade)
    rows = []
    for key, items in buckets.items():
        stats = _trade_stats(items)
        row: dict[str, Any] = {**stats}
        if len(key) == 5:
            row.update(
                {
                    "candidate_type": key[0],
                    "candidate_key": key[1],
                    "symbol": key[2],
                    "timeframe": key[3],
                    "direction": key[4],
                }
            )
        else:
            row.update({"symbol": key[0], "direction": key[1]})
        rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            row["closed_trades"],
            row["profit_factor"] if row["profit_factor"] is not None else -999.0,
            row["avg_pnl"] if row["avg_pnl"] is not None else -999.0,
            row["total_trades"],
        ),
        reverse=True,
    )


def _candidate_group_key(trade: ShadowPaperTrade) -> tuple[str, str, str, str, str]:
    return (trade.candidate_type, trade.candidate_key, trade.symbol, trade.timeframe, trade.direction)


def _symbol_group_key(trade: ShadowPaperTrade) -> tuple[str, str]:
    return (trade.symbol, trade.direction)


async def _latest_price(session: AsyncSession, symbol: str) -> float | None:
    row = await session.scalar(
        select(PriceSnapshot.price).where(PriceSnapshot.symbol == symbol).order_by(PriceSnapshot.created_at.desc()).limit(1)
    )
    return float(row) if isinstance(row, (int, float)) else None


async def _open_shadow_trade(
    session: AsyncSession,
    *,
    candidate_key: str,
    symbol: str,
) -> ShadowPaperTrade | None:
    return await session.scalar(
        select(ShadowPaperTrade)
        .where(
            ShadowPaperTrade.strategy_name == SHADOW_STRATEGY_NAME,
            ShadowPaperTrade.candidate_key == candidate_key,
            ShadowPaperTrade.symbol == symbol,
            ShadowPaperTrade.status == "open",
        )
        .limit(1)
    )


def _shadow_levels(direction: str, entry_price: float, asset_tier: str) -> dict[str, float]:
    template = template_for_tier(asset_tier)
    if direction == "long":
        return {
            "stop_loss": entry_price * (1 - template.stop_pct),
            "take_profit": entry_price * (1 + template.target_pct),
        }
    return {
        "stop_loss": entry_price * (1 + template.stop_pct),
        "take_profit": entry_price * (1 - template.target_pct),
    }


def _execution_price(direction: str, price: float, *, side: str, asset_tier: str) -> float:
    slippage = _slippage_rate(asset_tier)
    if side == "entry":
        worse = 1 + slippage if direction == "long" else 1 - slippage
    else:
        worse = 1 - slippage if direction == "long" else 1 + slippage
    return price * worse


def _net_pnl(trade: ShadowPaperTrade, price: float, *, exit_side: str) -> float:
    gross = _pnl(trade.direction, trade.entry_price, price)
    fee_cost = SHADOW_FEE_RATE if exit_side == "mark" else SHADOW_FEE_RATE * 2
    return gross - fee_cost


def _slippage_rate(asset_tier: str) -> float:
    return SHADOW_SLIPPAGE_RATE_BY_TIER.get(asset_tier, SHADOW_SLIPPAGE_RATE_BY_TIER["high_volatility"])


def _execution_model(asset_tier: str) -> dict[str, Any]:
    return {
        "fee_rate_per_side": SHADOW_FEE_RATE,
        "round_trip_fee_rate": SHADOW_FEE_RATE * 2,
        "slippage_rate": _slippage_rate(asset_tier),
        "entry_and_exit_use_worse_price": True,
        "mode": "conservative_shadow_paper",
    }


def _merge_context(current: dict[str, Any] | None, updates: dict[str, Any]) -> dict[str, Any]:
    payload = dict(current or {})
    execution_model = payload.get("execution_model") if isinstance(payload.get("execution_model"), dict) else {}
    payload.update(updates)
    if execution_model and "execution_model" not in updates:
        payload["execution_model"] = execution_model
    return payload


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


def _promotion_report(candidate_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for row in candidate_rows:
        status, blockers = _promotion_status(row)
        rows.append({**row, "promotion_status": status, "promotion_blockers": blockers})
    return {
        "criteria": {
            "min_closed_trades": PROMOTION_MIN_CLOSED_TRADES,
            "min_win_rate": PROMOTION_MIN_WIN_RATE,
            "min_profit_factor": PROMOTION_MIN_PROFIT_FACTOR,
            "max_drawdown": PROMOTION_MAX_DRAWDOWN,
            "cost_model_required": True,
        },
        "ready": [row for row in rows if row["promotion_status"] == "ready_for_paper_weight"],
        "watchlist": [row for row in rows if row["promotion_status"] == "watchlist"],
        "rejected": [row for row in rows if row["promotion_status"] == "reject_or_pause"],
        "all": rows,
    }


def _promotion_status(row: dict[str, Any]) -> tuple[str, list[str]]:
    blockers: list[str] = []
    closed = int(row.get("closed_trades") or 0)
    win_rate = row.get("win_rate")
    profit_factor = row.get("profit_factor")
    max_drawdown = float(row.get("max_drawdown") or 0.0)
    if closed < PROMOTION_MIN_CLOSED_TRADES:
        blockers.append("insufficient_closed_trades")
    if not isinstance(win_rate, (int, float)) or float(win_rate) < PROMOTION_MIN_WIN_RATE:
        blockers.append("win_rate_below_threshold")
    if not isinstance(profit_factor, (int, float)) or float(profit_factor) < PROMOTION_MIN_PROFIT_FACTOR:
        blockers.append("profit_factor_below_threshold")
    if max_drawdown > PROMOTION_MAX_DRAWDOWN:
        blockers.append("drawdown_above_threshold")
    if not blockers:
        return "ready_for_paper_weight", []
    if closed >= PROMOTION_MIN_CLOSED_TRADES:
        return "reject_or_pause", blockers
    if closed >= max(5, PROMOTION_MIN_CLOSED_TRADES // 3) and blockers != ["insufficient_closed_trades"]:
        return "watchlist", blockers
    return "observing", blockers


def _asset_tier(symbol: str) -> str:
    coin = symbol.removesuffix("USDT")
    if coin in {"BTC", "ETH"}:
        return "core"
    if coin in {"SOL", "BNB", "LINK", "TON"}:
        return "mainstream"
    return "high_volatility"


def _signal_key(*, candidate_key: str, symbol: str, direction: str, price: float) -> str:
    raw = f"{SHADOW_STRATEGY_NAME}:{candidate_key}:{symbol}:{direction}:{round(price, 6)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _candidate_context(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_key": row.get("segment_key"),
        "feature_key": row.get("feature_key"),
        "sample_count": row.get("sample_count"),
        "raw_sample_count": row.get("raw_sample_count"),
        "win_rate": row.get("win_rate"),
        "win_rate_lower": row.get("win_rate_lower"),
        "avg_return": row.get("avg_return"),
        "avg_return_lower": row.get("avg_return_lower"),
        "profit_factor": row.get("profit_factor"),
        "profit_factor_lower": row.get("profit_factor_lower"),
        "reliability_score": row.get("reliability_score"),
        "time_split": row.get("time_split"),
        "overfit_risk": row.get("overfit_risk"),
        "promotion_status": row.get("promotion_status"),
        "rejection_reasons": row.get("rejection_reasons"),
    }


def _trade_payload(trade: ShadowPaperTrade) -> dict[str, Any]:
    return {
        "id": trade.id,
        "strategy_name": trade.strategy_name,
        "candidate_type": trade.candidate_type,
        "candidate_key": trade.candidate_key,
        "symbol": trade.symbol,
        "timeframe": trade.timeframe,
        "direction": trade.direction,
        "entry_price": trade.entry_price,
        "stop_loss": trade.stop_loss,
        "take_profit": trade.take_profit,
        "status": trade.status,
        "exit_price": trade.exit_price,
        "exit_reason": trade.exit_reason,
        "pnl": trade.pnl,
        "r_multiple": trade.r_multiple,
        "mfe": trade.mfe,
        "mae": trade.mae,
        "opened_at": trade.opened_at,
        "closed_at": trade.closed_at,
        "source_experiment_run_id": trade.source_experiment_run_id,
        "context": trade.context,
    }


def _shadow_policy() -> dict[str, Any]:
    return {
        "research_only": True,
        "opens_paper_trades": False,
        "opens_live_orders": False,
        "sends_entry_notifications": False,
        "isolated_table": "shadow_paper_trades",
        "uses_fee_and_slippage": True,
        "default_execution_model": _execution_model("mixed"),
    }
