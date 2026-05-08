from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    closed = [trade for trade in trades if trade.status == "closed"]
    open_trades = [trade for trade in trades if trade.status == "open"]
    pnl_values = [float(trade.pnl or 0.0) for trade in closed]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total_pnl = sum(pnl_values)
    r_values = [float(trade.r_multiple) for trade in closed if trade.r_multiple is not None]
    open_mfe = [float(trade.mfe or 0.0) for trade in open_trades]
    open_mae = [float(trade.mae or 0.0) for trade in open_trades]
    return {
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "closed_trades": len(closed),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": len(wins) / len(closed) if closed else None,
        "avg_pnl": total_pnl / len(closed) if closed else None,
        "total_pnl": total_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": _profit_factor(gross_profit, gross_loss),
        "max_drawdown": _max_drawdown(pnl_values),
        "avg_r_multiple": sum(r_values) / len(r_values) if r_values else None,
        "best_trade": max(pnl_values) if pnl_values else None,
        "worst_trade": min(pnl_values) if pnl_values else None,
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
    if gross_loss:
        return gross_profit / gross_loss
    if gross_profit:
        return float("inf")
    return None


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        if peak:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return max_drawdown
