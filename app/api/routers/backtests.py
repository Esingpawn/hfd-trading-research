from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.api.shared import _latest_backtests_payload
from app.services.backtest_batch import run_backtest_batch

router = APIRouter()


@router.post("/backtests/batch")
async def backtest_batch(
    session: SessionDep,
    coins: list[str] | None = Query(default=None),
    timeframes: list[str] | None = Query(default=None),
    limit_zones: int = 100,
) -> dict[str, object]:
    return await run_backtest_batch(
        session=session,
        coins=coins,
        timeframes=timeframes,
        limit_zones=limit_zones,
        persist=True,
    )


@router.get("/backtests/latest")
async def latest_backtests(session: SessionDep) -> dict[str, object]:
    return await _latest_backtests_payload(session)
