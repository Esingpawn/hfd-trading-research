from __future__ import annotations

import os
import time
from datetime import timedelta

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import SessionDep
from app.api.shared import (
    APP_STARTED_AT,
    SERVER_MONOTONIC_STARTED_AT,
    _as_aware,
    _cached_completeness,
    _collection_run_payload,
    _completeness_cache_info,
    _latest_backtests_payload,
    _latest_collection_run,
    _latest_created_at,
    _market_cache_info,
    _runtime_interval_seconds,
    _runtime_process_payload,
)
from app.application.storage import get_storage_health, run_storage_maintenance
from app.constants import ASSETS, CORE_INDICATORS, TIMEFRAMES
from app.models import PaperTrade, PriceSnapshot, SignalSnapshot, StrategyDecision
from app.services.data_quality import data_quality_report
from app.services.diagnostics import build_diagnostics
from app.services.telegram import TelegramClient

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config/universe")
async def universe() -> dict[str, object]:
    return {
        "assets": {key: vars(value) for key, value in ASSETS.items()},
        "timeframes": {key: vars(value) for key, value in TIMEFRAMES.items()},
        "indicators": list(CORE_INDICATORS),
    }


@router.get("/system/summary")
async def system_summary(
    session: SessionDep,
    include_details: bool = Query(default=False),
) -> dict[str, object]:
    snapshot_count = (
        await session.execute(select(func.count()).select_from(SignalSnapshot))
    ).scalar_one()
    price_count = (
        await session.execute(select(func.count()).select_from(PriceSnapshot))
    ).scalar_one()
    decision_count = (
        await session.execute(select(func.count()).select_from(StrategyDecision))
    ).scalar_one()
    open_trade_count = (
        await session.execute(
            select(func.count()).select_from(PaperTrade).where(PaperTrade.status == "open")
        )
    ).scalar_one()
    latest_collection = await _latest_collection_run(session)
    result: dict[str, object] = {
        "snapshots": snapshot_count,
        "prices": price_count,
        "decisions": decision_count,
        "open_trades": open_trade_count,
        "mode": "paper_trading",
        "started_at": APP_STARTED_AT,
        "latest": {
            "collection": _collection_run_payload(latest_collection),
            "signal_snapshot_at": await _latest_created_at(session, SignalSnapshot),
            "price_snapshot_at": await _latest_created_at(session, PriceSnapshot),
            "decision_at": await _latest_created_at(session, StrategyDecision),
        },
    }
    if include_details:
        latest_backtest = await _latest_backtests_payload(session)
        completeness_payload = await _cached_completeness(session)
        result["telegram"] = (await TelegramClient().status()).__dict__
        result["best_backtest"] = (latest_backtest.get("results") or [{}])[0]
        result["coverage"] = completeness_payload["summary"]
    return result


@router.get("/system/runtime")
async def system_runtime(session: SessionDep) -> dict[str, object]:
    latest_collection = await _latest_collection_run(session)
    collector = _runtime_process_payload("collect-core-loop", "分层采集循环")
    paper_loop = _runtime_process_payload("paper-loop", "纸上交易循环")
    interval_seconds = _runtime_interval_seconds(collector, default=1800)
    next_collect_at = None
    if latest_collection and latest_collection.finished_at:
        next_collect_at = _as_aware(latest_collection.finished_at) + timedelta(
            seconds=interval_seconds
        )
    payload = {
        "server": {
            "name": "FastAPI 面板服务",
            "pid": os.getpid(),
            "running": True,
            "started_at": APP_STARTED_AT,
            "uptime_seconds": round(time.monotonic() - SERVER_MONOTONIC_STARTED_AT, 1),
        },
        "collector": collector,
        "paper_loop": paper_loop,
        "market_cache": _market_cache_info(),
        "completeness_cache": _completeness_cache_info(),
        "collection": {
            "latest": _collection_run_payload(latest_collection),
            "interval_seconds": interval_seconds,
            "next_collect_at": next_collect_at,
        },
        "latest": {
            "signal_snapshot_at": await _latest_created_at(session, SignalSnapshot),
            "price_snapshot_at": await _latest_created_at(session, PriceSnapshot),
            "decision_at": await _latest_created_at(session, StrategyDecision),
        },
    }
    completeness_payload = await _cached_completeness(session)
    payload["diagnostics"] = build_diagnostics(payload, completeness_payload)
    return payload


@router.get("/system/storage")
async def system_storage(session: SessionDep) -> dict[str, object]:
    return await get_storage_health(session)


@router.post("/system/storage/indexes")
async def system_storage_indexes(session: SessionDep) -> dict[str, object]:
    result = await run_storage_maintenance(session, indexes=True)
    return result["actions"]["indexes"]


@router.post("/system/storage/checkpoint")
async def system_storage_checkpoint(
    session: SessionDep,
    truncate: bool = Query(default=True),
) -> dict[str, object]:
    result = await run_storage_maintenance(
        session,
        checkpoint=True,
        passive_checkpoint=not truncate,
    )
    return result["actions"]["checkpoint"]


@router.post("/system/storage/optimize")
async def system_storage_optimize(session: SessionDep) -> dict[str, object]:
    result = await run_storage_maintenance(session, optimize=True)
    return result["actions"]["optimize"]


@router.get("/data/completeness")
async def completeness(session: SessionDep) -> dict[str, object]:
    return await _cached_completeness(session)


@router.get("/data/quality-report")
async def quality_report(session: SessionDep) -> dict[str, object]:
    return await data_quality_report(session)
