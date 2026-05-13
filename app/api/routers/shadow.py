from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.services.shadow_paper import (
    mark_shadow_paper_trades,
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


@router.get("/shadow-paper/trades")
async def list_shadow_paper_trades(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, object]]:
    return await shadow_paper_trades(session, limit=limit)


@router.get("/shadow-paper/stats")
async def get_shadow_paper_stats(session: SessionDep) -> dict[str, object]:
    return await shadow_paper_stats(session)
