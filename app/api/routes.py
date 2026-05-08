from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    ASSETS,
    CORE_INDICATORS,
    EXPERIMENT_INDICATORS,
    REQUIRED_SCORING_INDICATORS,
    TIMEFRAMES,
)
from app.db import SessionLocal
from app.models import (
    BacktestRun,
    CollectionRun,
    PaperTrade,
    PriceSnapshot,
    SignalSnapshot,
    StrategyDecision,
)
from app.services.backtest_batch import run_backtest_batch
from app.services.completeness import data_completeness
from app.services.collector import SnapshotCollector
from app.services.diagnostics import build_diagnostics
from app.services.experiment_effectiveness import experiment_feature_effectiveness
from app.services.indicator_catalog import indicator_experiment_coverage
from app.services.paper import mark_open_trades, paper_scan
from app.services.paper_review import paper_trade_review
from app.services.paper_stats import paper_trade_stats
from app.services.signal_attribution import backfill_signal_outcomes, signal_effectiveness
from app.services.signal_weights import signal_weight_governance, build_signal_weight_map
from app.services.strategy import _score_states, _snapshot_is_fresh, _state_from_snapshot, evaluate_symbol
from app.services.telegram import TelegramClient, extract_chat_candidates

router = APIRouter()
DASHBOARD_HTML = Path(__file__).resolve().parents[1] / "web" / "dashboard.html"
RUNTIME_DIR = Path("data/runtime")
_APP_STARTED_AT = datetime.now(timezone.utc)
_SERVER_MONOTONIC_STARTED_AT = time.monotonic()
_MARKET_CACHE: tuple[float, str | None, list[dict[str, object]]] | None = None
_MARKET_CACHE_SECONDS = 60.0
_COMPLETENESS_CACHE: tuple[float, str | None, dict[str, object]] | None = None
_COMPLETENESS_CACHE_SECONDS = 30.0


async def get_session():
    async with SessionLocal() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


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


@router.get("/snapshots")
async def recent_snapshots(
    session: SessionDep,
    limit: int = Query(default=20, ge=1, le=200),
) -> list[dict[str, object]]:
    rows = await session.execute(
        select(SignalSnapshot).order_by(SignalSnapshot.created_at.desc()).limit(limit)
    )
    snapshots = rows.scalars().all()
    return [
        {
            "id": item.id,
            "symbol": item.symbol,
            "asset_tier": item.asset_tier,
            "timeframe": item.timeframe,
            "interval": item.interval,
            "indicator": item.indicator,
            "collected_at": item.collected_at,
            "summary": item.summary_payload,
        }
        for item in snapshots
    ]


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
        "started_at": _APP_STARTED_AT,
        "latest": {
            "collection": _collection_run_payload(latest_collection),
            "signal_snapshot_at": await _latest_created_at(session, SignalSnapshot),
            "price_snapshot_at": await _latest_created_at(session, PriceSnapshot),
            "decision_at": await _latest_created_at(session, StrategyDecision),
        },
    }
    if include_details:
        latest_backtest = await latest_backtests(session)
        completeness_payload = await _cached_completeness(session)
        result["telegram"] = await telegram_status()
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
            "started_at": _APP_STARTED_AT,
            "uptime_seconds": round(time.monotonic() - _SERVER_MONOTONIC_STARTED_AT, 1),
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
    payload["diagnostics"] = build_diagnostics(
        payload,
        completeness_payload,
    )
    return payload


@router.get("/data/completeness")
async def completeness(session: SessionDep) -> dict[str, object]:
    return await _cached_completeness(session)


@router.get("/market/overview")
async def market_overview(session: SessionDep) -> list[dict[str, object]]:
    cache_token = await _latest_collection_cache_token(session)
    cached = _market_cache_get(cache_token)
    if cached is not None:
        return cached
    symbols = [f"{coin}USDT" for coin in ASSETS]
    prices = await _latest_prices(session, symbols)
    snapshots = await _latest_snapshots_for_symbols(session, symbols)
    confirmations = await _confirmation_snapshots(session, symbols)
    signal_weights = await build_signal_weight_map(session)
    rows: list[dict[str, object]] = []
    for coin, asset in ASSETS.items():
        symbol = f"{coin}USDT"
        price = prices.get(symbol)
        states = [
            _state_from_snapshot(
                snapshots.get((symbol, timeframe_name, "smart_money_cost")),
                timeframe_name,
                timeframe.interval,
                price,
            )
            for timeframe_name, timeframe in TIMEFRAMES.items()
        ]
        snapshots_by_indicator: dict[str, SignalSnapshot] = {}
        stale_indicators: list[str] = []
        for indicator in CORE_INDICATORS:
            snapshot = snapshots.get((symbol, "*", indicator))
            if snapshot is None:
                continue
            if _snapshot_is_fresh(snapshot):
                snapshots_by_indicator[indicator] = snapshot
            else:
                stale_indicators.append(indicator)
        evaluation = _score_states(
            symbol,
            asset.tier,
            price,
            states,
            snapshots_by_indicator,
            stale_indicators=stale_indicators,
            signal_weights=signal_weights,
        )
        confirmation = _confirmation_from_items(
            confirmations.get(symbol, []),
            evaluation.direction,
        )
        evaluation.risk_payload["confirmation"] = confirmation
        states = {state.timeframe: state for state in evaluation.states}
        rows.append(
            {
                "symbol": f"{coin}USDT",
                "coin": coin,
                "tier": asset.tier,
                "direction": evaluation.direction,
                "score": evaluation.score,
                "weighted_score": (evaluation.risk_payload.get("score_breakdown") or {}).get("weighted_score"),
                "structure_score": (evaluation.risk_payload.get("score_breakdown") or {}).get("structure_score"),
                "execution_score": (evaluation.risk_payload.get("score_breakdown") or {}).get("execution_score"),
                "decision": evaluation.decision,
                "price": evaluation.price,
                "risk": evaluation.risk_payload,
                "reason": evaluation.reason,
                "modules": evaluation.modules,
                "warnings": evaluation.reason.get("warnings", []),
                "required_missing_indicators": evaluation.reason.get(
                    "required_missing_indicators",
                    evaluation.reason.get("missing_indicators", []),
                ),
                "stale_indicators": evaluation.reason.get("stale_indicators", []),
                "missing_indicators": evaluation.reason.get("missing_indicators", []),
                "short": states.get("short").bias if states.get("short") else "missing",
                "mid": states.get("mid").bias if states.get("mid") else "missing",
                "long": states.get("long").bias if states.get("long") else "missing",
            }
        )
    _market_cache_set(rows, cache_token)
    return rows


def _market_cache_get(cache_token: str | None) -> list[dict[str, object]] | None:
    if _MARKET_CACHE is None:
        return None
    created_at, cached_token, rows = _MARKET_CACHE
    if cached_token != cache_token:
        return None
    if time.monotonic() - created_at > _MARKET_CACHE_SECONDS:
        return None
    return rows


def _market_cache_set(rows: list[dict[str, object]], cache_token: str | None) -> None:
    global _MARKET_CACHE
    _MARKET_CACHE = (time.monotonic(), cache_token, rows)


def _market_cache_clear() -> None:
    global _MARKET_CACHE
    _MARKET_CACHE = None


def _market_cache_info() -> dict[str, object]:
    if _MARKET_CACHE is None:
        return {
            "enabled": True,
            "cached": False,
            "age_seconds": None,
            "ttl_seconds": _MARKET_CACHE_SECONDS,
            "remaining_seconds": 0,
        }
    created_at, cache_token, _rows = _MARKET_CACHE
    age = max(time.monotonic() - created_at, 0)
    remaining = max(_MARKET_CACHE_SECONDS - age, 0)
    return {
        "enabled": True,
        "cached": age <= _MARKET_CACHE_SECONDS,
        "age_seconds": round(age, 1),
        "ttl_seconds": _MARKET_CACHE_SECONDS,
        "remaining_seconds": round(remaining, 1),
        "collection_token": cache_token,
    }


async def _cached_completeness(session: AsyncSession) -> dict[str, object]:
    cache_token = await _latest_collection_cache_token(session)
    cached = _completeness_cache_get(cache_token)
    if cached is not None:
        return cached
    payload = await data_completeness(session)
    _completeness_cache_set(payload, cache_token)
    return payload


def _completeness_cache_get(cache_token: str | None) -> dict[str, object] | None:
    if _COMPLETENESS_CACHE is None:
        return None
    created_at, cached_token, payload = _COMPLETENESS_CACHE
    if cached_token != cache_token:
        return None
    if time.monotonic() - created_at > _COMPLETENESS_CACHE_SECONDS:
        return None
    return payload


def _completeness_cache_set(payload: dict[str, object], cache_token: str | None) -> None:
    global _COMPLETENESS_CACHE
    _COMPLETENESS_CACHE = (time.monotonic(), cache_token, payload)


def _completeness_cache_clear() -> None:
    global _COMPLETENESS_CACHE
    _COMPLETENESS_CACHE = None


def _completeness_cache_info() -> dict[str, object]:
    if _COMPLETENESS_CACHE is None:
        return {
            "enabled": True,
            "cached": False,
            "age_seconds": None,
            "ttl_seconds": _COMPLETENESS_CACHE_SECONDS,
            "remaining_seconds": 0,
        }
    created_at, cache_token, _payload = _COMPLETENESS_CACHE
    age = max(time.monotonic() - created_at, 0)
    remaining = max(_COMPLETENESS_CACHE_SECONDS - age, 0)
    return {
        "enabled": True,
        "cached": age <= _COMPLETENESS_CACHE_SECONDS,
        "age_seconds": round(age, 1),
        "ttl_seconds": _COMPLETENESS_CACHE_SECONDS,
        "remaining_seconds": round(remaining, 1),
        "collection_token": cache_token,
    }


async def _latest_collection_run(session: AsyncSession) -> CollectionRun | None:
    rows = await session.execute(
        select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(1)
    )
    return rows.scalar_one_or_none()


async def _latest_collection_cache_token(session: AsyncSession) -> str | None:
    run = await _latest_collection_run(session)
    if run is None:
        return None
    finished_at = _as_aware(run.finished_at).isoformat() if run.finished_at else "running"
    return f"{run.id}:{finished_at}:{run.snapshots_written}:{run.prices_written}"


async def _latest_created_at(
    session: AsyncSession,
    model: type[SignalSnapshot] | type[PriceSnapshot] | type[StrategyDecision],
) -> datetime | None:
    rows = await session.execute(select(func.max(model.created_at)))
    value = rows.scalar_one_or_none()
    return _as_aware(value) if value else None


def _collection_run_payload(run: CollectionRun | None) -> dict[str, object] | None:
    if run is None:
        return None
    finished_at = _as_aware(run.finished_at) if run.finished_at else None
    started_at = _as_aware(run.started_at)
    return {
        "id": run.id,
        "status": run.status,
        "dry_run": run.dry_run,
        "assets": run.requested_assets,
        "timeframes": run.requested_timeframes,
        "indicators": run.requested_indicators,
        "snapshots_written": run.snapshots_written,
        "prices_written": run.prices_written,
        "error_count": len(run.errors or []),
        "started_at": started_at,
        "finished_at": finished_at,
        "age_seconds": _age_seconds(finished_at or started_at),
    }


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _age_seconds(value: datetime | None) -> float | None:
    if value is None:
        return None
    now = datetime.now(timezone.utc)
    return round(max((now - _as_aware(value)).total_seconds(), 0), 1)


def _runtime_process_payload(name: str, label: str) -> dict[str, object]:
    meta = _runtime_meta(name)
    pid = _runtime_pid(name, meta)
    stdout_log = meta.get("stdout_log") or str(Path("data/logs") / f"{name}.out.log")
    stderr_log = meta.get("stderr_log") or str(Path("data/logs") / f"{name}.err.log")
    started_at = meta.get("started_at")
    return {
        "name": label,
        "pid": pid,
        "running": _pid_running(pid),
        "started_at": started_at,
        "interval_seconds": meta.get("interval_seconds"),
        "coins": meta.get("coins"),
        "timeframes": meta.get("timeframes"),
        "indicators": meta.get("indicators"),
        "mode": meta.get("mode"),
        "research_indicators": meta.get("research_indicators"),
        "research_intervals": meta.get("research_intervals"),
        "pid_file": str(RUNTIME_DIR / f"{name}.pid"),
        "stdout_log": stdout_log,
        "stderr_log": stderr_log,
        "last_log_line": _last_log_line(Path(stdout_log), started_at),
        "last_error_line": _last_log_line(Path(stderr_log), started_at),
    }


def _runtime_meta(name: str) -> dict[str, object]:
    path = RUNTIME_DIR / f"{name}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def _runtime_pid(name: str, meta: dict[str, object]) -> int | None:
    raw_pid = meta.get("pid")
    if raw_pid:
        try:
            return int(raw_pid)
        except (TypeError, ValueError):
            pass
    path = RUNTIME_DIR / f"{name}.pid"
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError):
        return None


def _runtime_interval_seconds(payload: dict[str, object], default: int) -> int:
    try:
        return int(payload.get("interval_seconds") or default)
    except (TypeError, ValueError):
        return default


def _pid_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_pid_running(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _windows_pid_running(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return f'"{pid}"' in result.stdout


def _last_log_line(path: Path, started_at: object = None) -> str | None:
    if not path.exists():
        return None
    try:
        if not _log_written_after_start(path, started_at):
            return None
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(size - 4096, 0))
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [line.strip() for line in chunk.splitlines() if line.strip()]
    return lines[-1][-500:] if lines else None


def _log_written_after_start(path: Path, started_at: object) -> bool:
    if not started_at:
        return True
    try:
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except (OSError, ValueError):
        return True
    return modified >= _as_aware(start)


async def _latest_prices(session: AsyncSession, symbols: list[str]) -> dict[str, float]:
    ranked = (
        select(
            PriceSnapshot.id.label("id"),
            func.row_number()
            .over(
                partition_by=PriceSnapshot.symbol,
                order_by=PriceSnapshot.created_at.desc(),
            )
            .label("rn"),
        )
        .where(PriceSnapshot.symbol.in_(symbols))
        .subquery()
    )
    rows = await session.execute(
        select(PriceSnapshot)
        .join(ranked, PriceSnapshot.id == ranked.c.id)
        .where(ranked.c.rn == 1)
    )
    return {item.symbol: item.price for item in rows.scalars()}


async def _latest_snapshots_for_symbols(
    session: AsyncSession,
    symbols: list[str],
) -> dict[tuple[str, str, str], SignalSnapshot]:
    ranked = (
        select(
            SignalSnapshot.id.label("id"),
            func.row_number()
            .over(
                partition_by=(
                    SignalSnapshot.symbol,
                    SignalSnapshot.timeframe,
                    SignalSnapshot.indicator,
                ),
                order_by=SignalSnapshot.created_at.desc(),
            )
            .label("rn"),
        )
        .where(SignalSnapshot.symbol.in_(symbols))
        .subquery()
    )
    rows = await session.execute(
        select(SignalSnapshot)
        .join(ranked, SignalSnapshot.id == ranked.c.id)
        .where(ranked.c.rn == 1)
        .order_by(SignalSnapshot.created_at.desc())
    )
    snapshots: dict[tuple[str, str, str], SignalSnapshot] = {}
    for item in rows.scalars():
        snapshots.setdefault((item.symbol, item.timeframe, item.indicator), item)
        snapshots.setdefault((item.symbol, "*", item.indicator), item)
    return snapshots


async def _confirmation_snapshots(
    session: AsyncSession,
    symbols: list[str],
    limit_per_symbol: int = 2,
) -> dict[str, list[StrategyDecision]]:
    ranked = (
        select(
            StrategyDecision.id.label("id"),
            func.row_number()
            .over(
                partition_by=StrategyDecision.symbol,
                order_by=StrategyDecision.created_at.desc(),
            )
            .label("rn"),
        )
        .where(StrategyDecision.symbol.in_(symbols))
        .subquery()
    )
    rows = await session.execute(
        select(StrategyDecision)
        .join(ranked, StrategyDecision.id == ranked.c.id)
        .where(ranked.c.rn <= limit_per_symbol)
        .order_by(StrategyDecision.created_at.desc())
    )
    decisions: dict[str, list[StrategyDecision]] = {symbol: [] for symbol in symbols}
    for item in rows.scalars():
        bucket = decisions.setdefault(item.symbol, [])
        if len(bucket) < limit_per_symbol:
            bucket.append(item)
    return decisions


def _confirmation_from_items(
    decisions: list[StrategyDecision],
    direction: str,
    required: int = 2,
) -> dict[str, object]:
    streak = 0
    for item in decisions[:required]:
        gate = (item.risk_payload or {}).get("execution_gate") or {}
        if item.decision == "open" and item.direction == direction and gate.get("ready"):
            streak += 1
            continue
        break
    return {
        "required": required,
        "streak": streak,
        "confirmed": streak >= required,
        "label": "连续确认" if streak >= required else "等待连续确认",
    }


async def _confirmation_snapshot(
    session: AsyncSession,
    symbol: str,
    direction: str,
    required: int = 2,
) -> dict[str, object]:
    rows = await session.execute(
        select(StrategyDecision)
        .where(StrategyDecision.symbol == symbol)
        .order_by(StrategyDecision.created_at.desc())
        .limit(required)
    )
    decisions = rows.scalars().all()
    streak = 0
    for item in decisions:
        gate = (item.risk_payload or {}).get("execution_gate") or {}
        if item.decision == "open" and item.direction == direction and gate.get("ready"):
            streak += 1
            continue
        break
    return {
        "required": required,
        "streak": streak,
        "confirmed": streak >= required,
        "label": "连续确认" if streak >= required else "等待连续确认",
    }


@router.post("/collect/run")
async def run_collection(
    session: SessionDep,
    dry_run: bool = True,
    coins: list[str] | None = Query(default=None),
    timeframes: list[str] | None = Query(default=None),
    indicators: list[str] | None = Query(default=None),
) -> dict[str, object]:
    collector = SnapshotCollector(session)
    try:
        result = await collector.collect(
            assets=coins,
            timeframes=timeframes,
            indicators=indicators,
            dry_run=dry_run,
        )
        if not dry_run:
            _market_cache_clear()
            _completeness_cache_clear()
        return vars(result)
    finally:
        await collector.close()


@router.post("/collect/scoring-core")
async def collect_scoring_core(
    session: SessionDep,
    dry_run: bool = True,
    coins: list[str] | None = Query(default=None),
    timeframes: list[str] | None = Query(default=None),
) -> dict[str, object]:
    collector = SnapshotCollector(session)
    try:
        result = await collector.collect(
            assets=coins,
            timeframes=timeframes,
            indicators=list(REQUIRED_SCORING_INDICATORS),
            dry_run=dry_run,
        )
        if not dry_run:
            _market_cache_clear()
            _completeness_cache_clear()
        return vars(result)
    finally:
        await collector.close()


@router.post("/collect/experiments")
async def collect_experiments(
    session: SessionDep,
    dry_run: bool = True,
    coins: list[str] | None = Query(default=None),
    timeframes: list[str] | None = Query(default=None),
) -> dict[str, object]:
    collector = SnapshotCollector(session)
    try:
        result = await collector.collect(
            assets=coins,
            timeframes=timeframes,
            indicators=list(EXPERIMENT_INDICATORS),
            dry_run=dry_run,
        )
        if not dry_run:
            _market_cache_clear()
            _completeness_cache_clear()
        payload = vars(result)
        payload["policy"] = {
            "used_for_execution_weights": False,
            "used_for_opening_decisions": False,
            "note": "Experiment collection is research-only and does not change paper/live opening logic.",
        }
        return payload
    finally:
        await collector.close()


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
                "开仓理由": item.reason.get("explanation", []),
                "风险提示": item.reason.get("warnings", []),
                "触发规则": item.reason.get("rules", []),
            },
            "created_at": item.created_at,
        }
        for item in items
    ]


@router.post("/signals/backfill")
async def backfill_signals(
    session: SessionDep,
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, object]:
    result = await backfill_signal_outcomes(session, limit=limit)
    return result.__dict__


@router.get("/signals/effectiveness")
async def signals_effectiveness(
    session: SessionDep,
    min_samples: int = Query(default=1, ge=1, le=1000),
    horizon: str = Query(default="4h", pattern="^(30m|1h|4h|24h)$"),
) -> dict[str, object]:
    return await signal_effectiveness(session, min_samples=min_samples, horizon=horizon)


@router.get("/signals/weights")
async def signals_weights(
    session: SessionDep,
    min_samples: int = Query(default=30, ge=1, le=1000),
    horizon: str = Query(default="4h", pattern="^(30m|1h|4h|24h)$"),
) -> dict[str, object]:
    return await signal_weight_governance(session, min_samples=min_samples, horizon=horizon)


@router.get("/signals/experiments")
async def signals_experiments(session: SessionDep) -> dict[str, object]:
    return await indicator_experiment_coverage(session)


@router.get("/signals/experiment-effectiveness")
async def signals_experiment_effectiveness(
    session: SessionDep,
    min_samples: int = Query(default=5, ge=1, le=500),
    horizon: str = Query(default="4h", pattern="^(30m|1h|4h|24h)$"),
    limit_per_series: int = Query(default=80, ge=10, le=500),
) -> dict[str, object]:
    return await experiment_feature_effectiveness(
        session,
        horizon=horizon,
        min_samples=min_samples,
        limit_per_series=limit_per_series,
    )


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


@router.get("/telegram/status")
async def telegram_status() -> dict[str, object]:
    status = await TelegramClient().status()
    return status.__dict__


@router.get("/telegram/updates")
async def telegram_updates(limit: int = Query(default=10, ge=1, le=100)) -> dict[str, object]:
    updates = await TelegramClient().get_updates(limit=limit)
    return {"chats": extract_chat_candidates(updates), "update_count": len(updates)}


@router.post("/telegram/send")
async def telegram_send(
    text: str = Query(..., min_length=1),
    chat_id: str | None = Query(default=None),
) -> dict[str, object]:
    result = await TelegramClient().send_message(text, chat_id=chat_id)
    return {"message_id": result.get("message_id"), "date": result.get("date")}


@router.get("/backtests/latest")
async def latest_backtests(session: SessionDep) -> dict[str, object]:
    rows = await session.execute(
        select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(1)
    )
    item = rows.scalar_one_or_none()
    if item is None:
        return {"results": [], "errors": []}
    return {
        "id": item.id,
        "strategy": item.strategy,
        "status": item.status,
        "params": item.params,
        "results": item.results,
        "errors": item.errors,
        "created_at": item.created_at,
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> Response:
    return HTMLResponse(
        DASHBOARD_HTML.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def _dashboard_html() -> str:
    return f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HFD Research Dashboard</title>
  <style>
    :root {{ color-scheme: dark; --bg:#080c11; --panel:#0f151d; --panel2:#101923; --line:#233142; --text:#e6edf3; --muted:#8da2b8; --green:#10b981; --red:#f43f5e; --blue:#38bdf8; --amber:#fbbf24; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font-family: Arial, "Microsoft YaHei", sans-serif; }}
    header {{ height: 62px; display:flex; align-items:center; justify-content:space-between; padding:0 22px; border-bottom:1px solid var(--line); background:#070a0f; position:sticky; top:0; z-index:2; }}
    h1 {{ font-size: 20px; margin: 0; letter-spacing:0; display:flex; align-items:center; gap:10px; }}
    h1 span {{ color:#28e0ba; font-weight:800; }}
    main {{ max-width: 1440px; margin: 0 auto; padding: 18px 22px 40px; }}
    .toolbar {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
    button, select, input {{ background:#111827; color:var(--text); border:1px solid #2b3a4d; height:34px; padding:0 10px; border-radius:6px; }}
    button {{ cursor:pointer; }}
    button:hover {{ border-color:#4b6583; }}
    .grid {{ display:grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap:12px; margin:14px 0 20px; }}
    .metric {{ background:linear-gradient(180deg,#111923,#0c1219); border:1px solid var(--line); border-radius:8px; padding:14px; min-height:88px; }}
    .metric .label {{ color:var(--muted); font-size:12px; }}
    .metric .value {{ font-size:25px; margin-top:9px; font-weight:700; }}
    section {{ border-top: 1px solid var(--line); padding: 18px 0; }}
    h2 {{ font-size:15px; margin:0 0 10px; color:#dbeafe; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; background:#0b1118; }}
    th, td {{ border-bottom: 1px solid #1f2937; padding: 8px; text-align: left; vertical-align:top; }}
    th {{ color: var(--muted); font-weight:600; }}
    .split {{ display:grid; grid-template-columns: 1fr 1fr; gap:18px; }}
    .mainGrid {{ display:grid; grid-template-columns: 1.1fr .9fr; gap:18px; }}
    .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .chance {{ background:linear-gradient(180deg,#14191f,#0d131a); border:1px solid #314153; border-radius:8px; padding:16px; min-height:260px; }}
    .chanceTitle {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; }}
    .coinName {{ font-size:24px; font-weight:800; }}
    .pill {{ display:inline-flex; align-items:center; gap:6px; height:24px; padding:0 8px; border:1px solid #2a3a4c; border-radius:999px; color:#bcd0e4; font-size:12px; }}
    .kv {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }}
    .kv div {{ border-left:1px solid #263547; padding-left:12px; }}
    .kv b {{ display:block; font-size:20px; margin-top:8px; }}
    .log {{ white-space:pre-wrap; font-family:Consolas, monospace; color:#b6c5d6; background:#070a0f; border:1px solid var(--line); padding:10px; min-height:74px; border-radius:6px; }}
    .ok {{ color:var(--green); }}
    .bad {{ color:var(--red); }}
    .warn {{ color:#fbbf24; }}
    .muted {{ color:var(--muted); }}
    @media (max-width: 1000px) {{ .grid,.split,.mainGrid,.kv {{ grid-template-columns:1fr; }} header {{ height:auto; padding:12px; align-items:flex-start; gap:10px; flex-direction:column; }} }}
  </style>
</head>
<body>
  <header>
    <h1><span>HFD</span> 暗流交易研究台</h1>
    <div class="toolbar">
      <button onclick="refreshAll()">刷新</button>
      <button onclick="collectDryRun()">采集 Dry-run</button>
      <button onclick="collectCore(false)">补齐核心指标</button>
      <button onclick="runPaperScan(true)">纸上扫描</button>
      <button onclick="markTrades()">标记持仓</button>
      <button onclick="sendTelegram()">TG 测试</button>
    </div>
  </header>
  <main>
    <div class="grid">
      <div class="metric"><div class="label">Telegram</div><div id="tgMetric" class="value">...</div></div>
      <div class="metric"><div class="label">实时快照</div><div id="snapshotMetric" class="value">...</div></div>
      <div class="metric"><div class="label">纸上持仓</div><div id="tradeMetric" class="value">...</div></div>
      <div class="metric"><div class="label">最佳回测评分</div><div id="backtestMetric" class="value">...</div></div>
    </div>

    <section>
      <h2>操作区</h2>
      <div class="toolbar">
        <input id="coins" value="BTC ETH SOL BNB LINK TON DOGE HYPE ZEC" style="width:330px" />
        <select id="dryRun"><option value="true">dry-run</option><option value="false">写入纸上交易</option></select>
        <button onclick="runPaperScan(document.getElementById('dryRun').value==='true')">执行纸上扫描</button>
        <button onclick="loadTelegramUpdates()">获取 TG chat_id</button>
        <input id="tgText" value="HFD 系统测试消息" style="width:220px" />
      </div>
      <div id="opLog" class="log">等待操作...</div>
    </section>

    <section class="mainGrid">
      <div class="panel">
        <h2>市场观察</h2>
        <div id="market"></div>
      </div>
      <div class="chance">
        <div class="chanceTitle">
          <div><div class="muted">当前机会</div><div id="chanceCoin" class="coinName">暂无</div></div>
          <span id="chanceScore" class="pill">评分 --</span>
        </div>
        <div class="kv">
          <div><span class="muted">方向</span><b id="chanceDirection">--</b></div>
          <div><span class="muted">入场</span><b id="chanceEntry">--</b></div>
          <div><span class="muted">止损</span><b id="chanceStop">--</b></div>
          <div><span class="muted">止盈</span><b id="chanceTarget">--</b></div>
        </div>
        <p id="chanceReason" class="muted" style="margin-top:18px;line-height:1.6">等待三周期信号。</p>
      </div>
    </section>

    <section class="panel">
      <h2>数据完整性</h2>
      <div id="coverage"></div>
    </section>

    <section class="split">
      <div class="panel"><h2>回测排行榜</h2><div id="backtests"></div></div>
      <div class="panel"><h2>Telegram Chats</h2><div id="tgChats"></div></div>
    </section>

    <section class="split">
      <div class="panel"><h2>纸上交易记录</h2><div id="trades"></div></div>
      <div class="panel"><h2>最新决策</h2><div id="decisions"></div></div>
    </section>
  </main>
  <script>
    const qs = (id) => document.getElementById(id);
    const fmt = (v) => typeof v === 'number' ? Number(v).toFixed(4) : (v ?? '');
    async function api(path, options={{}}) {{
      const res = await fetch(path, options);
      if (!res.ok) throw new Error(path + ' -> ' + res.status + ' ' + await res.text());
      return await res.json();
    }}
    function table(rows, cols) {{
      if (!rows || rows.length === 0) return '<p class="warn">暂无数据</p>';
      return '<table><thead><tr>' + cols.map(c=>'<th>'+c+'</th>').join('') + '</tr></thead><tbody>' +
        rows.map(r=>'<tr>'+cols.map(c=>'<td>'+fmt(r[c])+'</td>').join('')+'</tr>').join('') +
        '</tbody></table>';
    }}
    function log(obj) {{ qs('opLog').textContent = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2); }}
    async function refreshAll() {{
      try {{
        const [summary, tg, completeness, market, snaps, trades, decisions, backtests] = await Promise.all([
          api('/system/summary'),
          api('/telegram/status').catch(e=>({{configured:false,error:e.message}})),
          api('/data/completeness'),
          api('/market/overview'),
          api('/snapshots?limit=30'),
          api('/paper/trades?limit=30'),
          api('/decisions?limit=30'),
          api('/backtests/latest').catch(e=>({{results:[]}})),
        ]);
        qs('tgMetric').innerHTML = tg.configured ? (tg.error ? '<span class="bad">异常</span>' : '<span class="ok">@'+tg.bot_username+'</span>') : '<span class="warn">未配置</span>';
        qs('snapshotMetric').innerHTML = (summary.snapshots || snaps.length) + '<span class="muted" style="font-size:12px"> / 覆盖 '+ Math.round((summary.coverage.coverage_pct || 0) * 100) +'%</span>';
        qs('tradeMetric').textContent = trades.filter(t=>t.status==='open').length + ' open';
        const best = (backtests.results || [])[0] || {{}};
        qs('backtestMetric').textContent = best.score ? Number(best.score).toFixed(1) : '--';
        qs('coverage').innerHTML = table(completeness.matrix.map(row=>({{
          coin: row.coin,
          tier: row.tier,
          coverage: Math.round(row.coverage_pct * 100) + '%',
          missing: row.missing_count,
          stale: row.stale_count,
          ready: row.ready ? '可评分' : '待补齐'
        }})), ['coin','tier','coverage','missing','stale','ready']);
        qs('market').innerHTML = table(market, ['coin','price','direction','short','mid','long','score','decision']);
        qs('trades').innerHTML = table(trades, ['symbol','direction','entry_price','stop_loss','take_profit','status','pnl','r_multiple']);
        qs('decisions').innerHTML = table(decisions, ['symbol','direction','score','decision','created_at']);
        qs('backtests').innerHTML = table((backtests.results || []).slice(0,10), ['coin','timeframe','trade_count','win_rate','profit_factor','max_drawdown_pct','score']);
        const chance = market.find(d=>d.decision==='open') || null;
        if (chance) {{
          qs('chanceCoin').textContent = chance.symbol;
          qs('chanceScore').textContent = '评分 ' + fmt(chance.score);
          qs('chanceDirection').innerHTML = chance.direction === 'long' ? '<span class="ok">做多</span>' : '<span class="bad">做空</span>';
          qs('chanceEntry').textContent = fmt(chance.risk.entry_price);
          qs('chanceStop').textContent = fmt(chance.risk.stop_loss);
          qs('chanceTarget').textContent = fmt(chance.risk.take_profit);
          const warnings = (chance.reason.warnings || []).slice(0,4).join('；') || '暂无';
          qs('chanceReason').textContent = (chance.reason.explanation || chance.reason.rules || []).join(' / ') + '。风险提示：' + warnings;
        }} else {{
          const bestMarket = market.slice().sort((a,b)=>(b.score||0)-(a.score||0))[0] || {{}};
          qs('chanceCoin').textContent = bestMarket.symbol || '暂无';
          qs('chanceScore').textContent = bestMarket.score !== undefined ? '观察 ' + fmt(bestMarket.score) : '评分 --';
          qs('chanceDirection').textContent = '--';
          qs('chanceEntry').textContent = '--';
          qs('chanceStop').textContent = '--';
          qs('chanceTarget').textContent = '--';
          qs('chanceReason').textContent = '暂无满足完整数据与多信号确认的高可信机会。请先补齐核心指标快照。';
        }}
      }} catch (e) {{ log(e.message); }}
    }}
    async function collectDryRun() {{
      try {{ log(await api('/collect/run?dry_run=true&coins=BTC&timeframes=short&indicators=smart_money_cost', {{method:'POST'}})); await refreshAll(); }}
      catch(e) {{ log(e.message); }}
    }}
    async function collectCore(dryRun) {{
      const coins = qs('coins').value.trim().split(/\\s+/).map(c=>'coins='+encodeURIComponent(c)).join('&');
      try {{ log(await api('/collect/scoring-core?dry_run='+dryRun+'&'+coins, {{method:'POST'}})); await refreshAll(); }}
      catch(e) {{ log(e.message); }}
    }}
    async function runPaperScan(dryRun) {{
      const coins = qs('coins').value.trim().split(/\\s+/).map(c=>'coins='+encodeURIComponent(c)).join('&');
      try {{ log(await api('/paper/scan?dry_run='+dryRun+'&'+coins, {{method:'POST'}})); await refreshAll(); }}
      catch(e) {{ log(e.message); }}
    }}
    async function markTrades() {{
      try {{ log(await api('/paper/mark', {{method:'POST'}})); await refreshAll(); }}
      catch(e) {{ log(e.message); }}
    }}
    async function loadTelegramUpdates() {{
      try {{
        const data = await api('/telegram/updates');
        qs('tgChats').innerHTML = table(data.chats, ['chat_id','type','username','first_name','last_text']);
        log(data);
      }} catch(e) {{ log(e.message); }}
    }}
    async function sendTelegram() {{
      try {{ log(await api('/telegram/send?text='+encodeURIComponent(qs('tgText').value), {{method:'POST'}})); }}
      catch(e) {{ log(e.message); }}
    }}
    refreshAll();
  </script>
</body>
</html>
"""
