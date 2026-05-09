from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BacktestRun, CollectionRun, PriceSnapshot, SignalSnapshot, StrategyDecision
from app.services.completeness import data_completeness


DASHBOARD_HTML = Path(__file__).resolve().parents[1] / "web" / "dashboard.html"
RUNTIME_DIR = Path("data/runtime")
APP_STARTED_AT = datetime.now(timezone.utc)
SERVER_MONOTONIC_STARTED_AT = time.monotonic()
MARKET_CACHE_SECONDS = 60.0
COMPLETENESS_CACHE_SECONDS = 30.0

_MARKET_CACHE: tuple[float, str | None, list[dict[str, object]]] | None = None
_COMPLETENESS_CACHE: tuple[float, str | None, dict[str, object]] | None = None


def _market_cache_get(cache_token: str | None) -> list[dict[str, object]] | None:
    if _MARKET_CACHE is None:
        return None
    created_at, cached_token, rows = _MARKET_CACHE
    if cached_token != cache_token:
        return None
    if time.monotonic() - created_at > MARKET_CACHE_SECONDS:
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
            "ttl_seconds": MARKET_CACHE_SECONDS,
            "remaining_seconds": 0,
        }
    created_at, cache_token, _rows = _MARKET_CACHE
    age = max(time.monotonic() - created_at, 0)
    remaining = max(MARKET_CACHE_SECONDS - age, 0)
    return {
        "enabled": True,
        "cached": age <= MARKET_CACHE_SECONDS,
        "age_seconds": round(age, 1),
        "ttl_seconds": MARKET_CACHE_SECONDS,
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
    if time.monotonic() - created_at > COMPLETENESS_CACHE_SECONDS:
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
            "ttl_seconds": COMPLETENESS_CACHE_SECONDS,
            "remaining_seconds": 0,
        }
    created_at, cache_token, _payload = _COMPLETENESS_CACHE
    age = max(time.monotonic() - created_at, 0)
    remaining = max(COMPLETENESS_CACHE_SECONDS - age, 0)
    return {
        "enabled": True,
        "cached": age <= COMPLETENESS_CACHE_SECONDS,
        "age_seconds": round(age, 1),
        "ttl_seconds": COMPLETENESS_CACHE_SECONDS,
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
    heartbeat_age_seconds = _runtime_heartbeat_age_seconds(meta)
    heartbeat_ttl_seconds = _runtime_heartbeat_ttl_seconds(meta)
    running = _pid_running(pid) or _runtime_heartbeat_running(
        heartbeat_age_seconds,
        heartbeat_ttl_seconds,
    )
    return {
        "name": label,
        "pid": pid,
        "running": running,
        "started_at": started_at,
        "heartbeat_at": meta.get("heartbeat_at"),
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "heartbeat_ttl_seconds": heartbeat_ttl_seconds,
        "interval_seconds": meta.get("interval_seconds"),
        "coins": meta.get("coins"),
        "timeframes": meta.get("timeframes"),
        "indicators": meta.get("indicators"),
        "mode": meta.get("mode"),
        "research_indicators": meta.get("research_indicators"),
        "research_intervals": meta.get("research_intervals"),
        "containerized": meta.get("containerized"),
        "command": meta.get("command"),
        "status": meta.get("status"),
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


def _runtime_heartbeat_age_seconds(meta: dict[str, object]) -> float | None:
    heartbeat_at = _parse_runtime_datetime(meta.get("heartbeat_at"))
    return _age_seconds(heartbeat_at) if heartbeat_at else None


def _runtime_heartbeat_ttl_seconds(meta: dict[str, object]) -> int:
    raw_ttl = meta.get("heartbeat_ttl_seconds")
    try:
        ttl = int(raw_ttl or 0)
        if ttl > 0:
            return ttl
    except (TypeError, ValueError):
        pass
    try:
        interval = int(meta.get("interval_seconds") or 0)
    except (TypeError, ValueError):
        interval = 0
    return max(interval * 2 + 60, 120)


def _runtime_heartbeat_running(age_seconds: float | None, ttl_seconds: int) -> bool:
    if age_seconds is None:
        return False
    return age_seconds <= ttl_seconds


def _parse_runtime_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return _as_aware(value)
    try:
        return _as_aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


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
        "label": "杩炵画纭" if streak >= required else "绛夊緟杩炵画纭",
    }


async def _latest_backtests_payload(session: AsyncSession) -> dict[str, object]:
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
