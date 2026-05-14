from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.services.shadow_paper import (
    mark_shadow_paper_trades,
    shadow_paper_promotion_report,
    shadow_paper_replay,
    shadow_paper_scan,
    shadow_paper_stats,
    shadow_paper_trades,
)

router = APIRouter()


@router.post("/shadow-paper/scan")
async def run_shadow_paper_scan(
    session: SessionDep,
    candidate_limit: int = Query(default=20, ge=1, le=200),
    include_watchlist: bool = Query(default=True),
) -> dict[str, object]:
    return await shadow_paper_scan(session, candidate_limit=candidate_limit, include_watchlist=include_watchlist)


@router.post("/shadow-paper/mark")
async def run_shadow_paper_mark(session: SessionDep) -> dict[str, object]:
    return await mark_shadow_paper_trades(session)


@router.post("/shadow-paper/replay")
async def run_shadow_paper_replay(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
    limit: int = Query(default=500, ge=1, le=5000),
    candidate_limit: int = Query(default=50, ge=1, le=500),
    include_watchlist: bool = Query(default=True),
) -> dict[str, object]:
    return await shadow_paper_replay(
        session,
        horizon=horizon,
        limit=limit,
        candidate_limit=candidate_limit,
        include_watchlist=include_watchlist,
    )


@router.get("/shadow-paper/trades")
async def list_shadow_paper_trades(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, object]]:
    return await shadow_paper_trades(session, limit=limit)


@router.get("/shadow-paper/stats")
async def get_shadow_paper_stats(session: SessionDep) -> dict[str, object]:
    return await shadow_paper_stats(session)


@router.get("/shadow-paper/promotion")
async def get_shadow_paper_promotion(session: SessionDep) -> dict[str, object]:
    return await shadow_paper_promotion_report(session)
