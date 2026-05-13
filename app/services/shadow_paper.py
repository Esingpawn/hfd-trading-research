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
        levels = _shadow_levels(direction, price, asset_tier)
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
            entry_price=price,
            stop_loss=levels["stop_loss"],
            take_profit=levels["take_profit"],
            position_size=1.0,
            status="open",
            context={
                "research_only": True,
                "opens_paper_trades": False,
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
        pnl = _pnl(trade.direction, trade.entry_price, price)
        trade.mfe = max(trade.mfe, pnl)
        trade.mae = min(trade.mae, pnl)
        exit_reason = _exit_reason(trade, price)
        if exit_reason:
            trade.status = "closed"
            trade.exit_price = price
            trade.exit_reason = exit_reason
            trade.pnl = pnl
            stop_pct = abs(trade.entry_price - trade.stop_loss) / trade.entry_price
            trade.r_multiple = pnl / stop_pct if stop_pct else 0.0
            trade.closed_at = datetime.now(timezone.utc)
            closed.append({"id": trade.id, "symbol": trade.symbol, "exit_reason": exit_reason, "pnl": pnl})
        else:
            updated.append({"id": trade.id, "symbol": trade.symbol, "pnl": pnl})
    if closed or updated:
        await session.commit()
    return {"closed": closed, "updated": updated, "policy": _shadow_policy()}


async def shadow_paper_trades(session: AsyncSession, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = await session.execute(select(ShadowPaperTrade).order_by(ShadowPaperTrade.opened_at.desc()).limit(limit))
    return [_trade_payload(item) for item in rows.scalars().all()]


async def shadow_paper_stats(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(select(ShadowPaperTrade))
    trades = rows.scalars().all()
    closed = [item for item in trades if item.status == "closed" and isinstance(item.pnl, (int, float))]
    wins = [item.pnl for item in closed if item.pnl and item.pnl > 0]
    losses = [item.pnl for item in closed if item.pnl and item.pnl < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "strategy_name": SHADOW_STRATEGY_NAME,
        "open_trades": sum(1 for item in trades if item.status == "open"),
        "closed_trades": len(closed),
        "total_trades": len(trades),
        "win_rate": len(wins) / len(closed) if closed else None,
        "avg_pnl": mean([float(item.pnl) for item in closed]) if closed else None,
        "profit_factor": gross_win / gross_loss if gross_loss else (999.0 if gross_win else None),
        "policy": _shadow_policy(),
    }


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
        "avg_return": row.get("avg_return"),
        "profit_factor": row.get("profit_factor"),
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
    }
