from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.trade_outcomes import max_drawdown, profit_factor, summarize_trade_outcomes
from app.models import PaperTrade


async def paper_trade_stats(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(select(PaperTrade).order_by(PaperTrade.opened_at))
    trades = list(rows.scalars().all())
    return summarize_paper_trades(trades)


def summarize_paper_trades(trades: list[PaperTrade]) -> dict[str, Any]:
    closed = [trade for trade in trades if trade.status == "closed"]
    open_trades = [trade for trade in trades if trade.status == "open"]
    sample_target = 200
    minimum_sample = 100
    overall = _group_stats(trades)
    overall.update(
        {
            "sample_ready": len(closed) >= minimum_sample,
            "sample_target": sample_target,
            "minimum_sample": minimum_sample,
            "sample_progress": round(min(len(closed) / sample_target, 1.0), 4),
            "by_symbol": _group_by(trades, lambda trade: trade.symbol),
            "by_direction": _group_by(trades, lambda trade: trade.direction),
            "by_tier": _group_by(trades, lambda trade: trade.asset_tier),
            "open_exposure": _open_exposure(open_trades),
        }
    )
    return overall


def _group_by(trades: list[PaperTrade], key_fn) -> list[dict[str, Any]]:
    groups: dict[str, list[PaperTrade]] = {}
    for trade in trades:
        key = str(key_fn(trade) or "unknown")
        groups.setdefault(key, []).append(trade)
    rows = []
    for key, items in groups.items():
        payload = _group_stats(items)
        payload["key"] = key
        rows.append(payload)
    return sorted(rows, key=lambda row: (-int(row["total_trades"]), row["key"]))


def _group_stats(trades: list[PaperTrade]) -> dict[str, Any]:
    open_trades = [trade for trade in trades if trade.status == "open"]
    open_mfe = [float(trade.mfe or 0.0) for trade in open_trades]
    open_mae = [float(trade.mae or 0.0) for trade in open_trades]
    return summarize_trade_outcomes(trades) | {
        "open_mfe": max(open_mfe) if open_mfe else None,
        "open_mae": min(open_mae) if open_mae else None,
    }


def _open_exposure(open_trades: list[PaperTrade]) -> list[dict[str, Any]]:
    return [
        {
            "trade_id": trade.id,
            "symbol": trade.symbol,
            "direction": trade.direction,
            "asset_tier": trade.asset_tier,
            "position_size": trade.position_size,
            "entry_price": trade.entry_price,
            "stop_loss": trade.stop_loss,
            "take_profit": trade.take_profit,
            "mfe": trade.mfe,
            "mae": trade.mae,
            "opened_at": trade.opened_at,
        }
        for trade in open_trades
    ]


def _profit_factor(gross_profit: float, gross_loss: float) -> float | None:
    return profit_factor(gross_profit, gross_loss)


def _max_drawdown(returns: list[float]) -> float:
    return max_drawdown(returns)
