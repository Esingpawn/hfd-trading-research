from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.deps import SessionDep
from app.api.shared import _market_cache_clear
from app.models import PaperTrade, StrategyDecision
from app.services.paper import mark_open_trades, paper_scan
from app.services.paper_review import paper_trade_review
from app.services.paper_stats import paper_trade_stats

router = APIRouter()


@router.post("/paper/scan")
async def run_paper_scan(
    session: SessionDep,
    dry_run: bool = True,
    coins: list[str] | None = Query(default=None),
    notify: bool = Query(default=False),
) -> dict[str, object]:
    selected = [coin.upper() for coin in coins] if coins else ["BTC", "ETH"]
    result = await paper_scan(session, selected, dry_run=dry_run, notify=notify)
    _market_cache_clear()
    return result.__dict__


@router.post("/paper/mark")
async def run_paper_mark(session: SessionDep) -> dict[str, object]:
    result = await mark_open_trades(session)
    _market_cache_clear()
    return result


@router.get("/paper/trades")
async def paper_trades(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, object]]:
    rows = await session.execute(
        select(PaperTrade).order_by(PaperTrade.opened_at.desc()).limit(limit)
    )
    trades = rows.scalars().all()
    return [
        {
            "id": trade.id,
            "symbol": trade.symbol,
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
        }
        for trade in trades
    ]


@router.get("/paper/trades/{trade_id}/review")
async def paper_review(trade_id: str, session: SessionDep) -> dict[str, object]:
    payload = await paper_trade_review(session, trade_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="paper trade not found")
    return payload


@router.get("/paper/stats")
async def paper_stats(session: SessionDep) -> dict[str, object]:
    return await paper_trade_stats(session)


@router.get("/decisions")
async def decisions(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, object]]:
    rows = await session.execute(
        select(StrategyDecision).order_by(StrategyDecision.created_at.desc()).limit(limit)
    )
    items = rows.scalars().all()
    return [
        {
            "id": item.id,
            "strategy": item.strategy_name,
            "version": item.strategy_version,
            "symbol": item.symbol,
            "direction": item.direction,
            "score": item.score,
            "decision": item.decision,
            "reason": item.reason,
            "risk": item.risk_payload,
            "journal": {
                "寮€浠撶悊鐢?": item.reason.get("explanation", []),
                "椋庨櫓鎻愮ず": item.reason.get("warnings", []),
                "瑙﹀彂瑙勫垯": item.reason.get("rules", []),
            },
            "created_at": item.created_at,
        }
        for item in items
    ]
