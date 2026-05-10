from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.queue import build_queue
from app.models import TaskRun
from app.services.collector import SnapshotCollector
from app.services.feature_candidates import feature_candidate_screen, feature_paper_ab
from app.services.features import (
    backfill_feature_events,
    backfill_feature_labels,
    reset_feature_research,
    refresh_feature_research,
)
from app.services.paper import mark_open_trades, paper_scan
from app.services.signal_attribution import backfill_signal_outcomes
from app.application.storage import run_storage_maintenance


async def enqueue_task(
    session: AsyncSession,
    *,
    task_name: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = TaskRun(task_name=task_name, payload=payload or {}, result={})
    session.add(item)
    await session.flush()
    queue_result = await build_queue().enqueue(task_name, {"task_run_id": item.id, **(payload or {})})
    item.result = {"queue": queue_result}
    if queue_result.get("status") == "queued":
        item.status = "queued"
    else:
        item.status = "recorded"
    await session.commit()
    return {"task_run_id": item.id, "status": item.status, "queue": queue_result}


async def recent_tasks(session: AsyncSession, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = await session.execute(select(TaskRun).order_by(TaskRun.queued_at.desc()).limit(limit))
    return [task_payload(item) for item in rows.scalars()]


async def run_task_by_id(session: AsyncSession, task_run_id: str) -> dict[str, Any]:
    item = await session.get(TaskRun, task_run_id)
    if item is None:
        raise ValueError(f"task_run not found: {task_run_id}")
    return await run_task_record(session, item)


async def run_task_record(session: AsyncSession, item: TaskRun) -> dict[str, Any]:
    item.status = "running"
    item.started_at = _utc_now()
    item.error = None
    await session.commit()
    try:
        result = await execute_task(session, item.task_name, item.payload or {})
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        item = await session.get(TaskRun, item.id)
        if item is None:
            raise
        item.status = "failed"
        item.error = str(exc)
        item.finished_at = _utc_now()
        await session.commit()
        raise
    item.status = "completed"
    item.result = _json_safe({**(item.result or {}), "execution": result})
    item.finished_at = _utc_now()
    await session.commit()
    return task_payload(item)


async def execute_task(session: AsyncSession, task_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if task_name in {"collect", "collect.run"}:
        collector = SnapshotCollector(session)
        try:
            result = await collector.collect(
                assets=payload.get("coins") or payload.get("assets"),
                timeframes=payload.get("timeframes"),
                indicators=payload.get("indicators"),
                dry_run=bool(payload.get("dry_run", False)),
            )
        finally:
            await collector.close()
        return result.__dict__
    if task_name in {"collect.scoring_core", "collect-scoring-core"}:
        from app.constants import REQUIRED_SCORING_INDICATORS

        collector = SnapshotCollector(session)
        try:
            result = await collector.collect(
                assets=payload.get("coins") or payload.get("assets"),
                timeframes=payload.get("timeframes"),
                indicators=list(REQUIRED_SCORING_INDICATORS),
                dry_run=bool(payload.get("dry_run", False)),
            )
        finally:
            await collector.close()
        return result.__dict__
    if task_name in {"paper.scan", "paper-scan"}:
        coins = [str(item).upper() for item in (payload.get("coins") or ["BTC", "ETH"])]
        result = await paper_scan(
            session,
            coins=coins,
            dry_run=bool(payload.get("dry_run", False)),
            notify=bool(payload.get("notify", False)),
        )
        return result.__dict__
    if task_name in {"paper.mark", "paper-mark"}:
        return await mark_open_trades(session)
    if task_name in {"signals.backfill", "signals-backfill"}:
        result = await backfill_signal_outcomes(session, limit=int(payload.get("limit") or 500))
        return result.__dict__
    if task_name in {"features.backfill", "features-backfill"}:
        result = await backfill_feature_events(
            session,
            limit=int(payload.get("limit") or 500),
            indicators=_optional_str_list(payload.get("indicators")),
        )
        return result.__dict__
    if task_name in {"features.label", "features-label"}:
        result = await backfill_feature_labels(
            session,
            limit=int(payload.get("limit") or 1000),
            horizons=_optional_str_list(payload.get("horizons")),
            refresh_labeled=bool(payload.get("refresh_labeled", False)),
        )
        return result.__dict__
    if task_name in {"features.reset", "features-reset"}:
        result = await reset_feature_research(
            session,
            indicators=_optional_str_list(payload.get("indicators")),
        )
        return result.__dict__
    if task_name in {"features.refresh", "features-refresh"}:
        return await refresh_feature_research(
            session,
            limit=int(payload.get("limit") or 500),
            indicators=_optional_str_list(payload.get("indicators")),
            horizons=_optional_str_list(payload.get("horizons")),
            min_samples=int(payload.get("min_samples") or 5),
        )
    if task_name in {"features.candidates", "features-candidates"}:
        return await feature_candidate_screen(
            session,
            horizon=_payload_str(payload, "horizon", "30m"),
            min_samples=_payload_int(payload, "min_samples", 30),
            min_win_rate=_payload_float(payload, "min_win_rate", 0.52),
            min_profit_factor=_payload_float(payload, "min_profit_factor", 1.2),
            min_avg_return=_payload_float(payload, "min_avg_return", 0.0),
            segment_min_samples=_payload_int(payload, "segment_min_samples", 5),
            min_segments=_payload_int(payload, "min_segments", 2),
            limit=_payload_int(payload, "limit", 20000),
            persist=_payload_bool(payload, "persist", True),
        )
    if task_name in {"features.paper_ab", "features-paper-ab"}:
        return await feature_paper_ab(
            session,
            horizon=_payload_str(payload, "horizon", "30m"),
            min_samples=_payload_int(payload, "min_samples", 30),
            min_win_rate=_payload_float(payload, "min_win_rate", 0.52),
            min_profit_factor=_payload_float(payload, "min_profit_factor", 1.2),
            min_avg_return=_payload_float(payload, "min_avg_return", 0.0),
            segment_min_samples=_payload_int(payload, "segment_min_samples", 5),
            min_segments=_payload_int(payload, "min_segments", 2),
            candidate_limit=_payload_int(payload, "candidate_limit", 20),
            limit=_payload_int(payload, "limit", 20000),
            persist=_payload_bool(payload, "persist", True),
        )
    if task_name in {"storage.maintain", "storage-maintain"}:
        return await run_storage_maintenance(
            session,
            indexes=bool(payload.get("indexes", False)),
            checkpoint=bool(payload.get("checkpoint", False)),
            passive_checkpoint=bool(payload.get("passive_checkpoint", False)),
            optimize=bool(payload.get("optimize", False)),
        )
    raise ValueError(f"unsupported task_name: {task_name}")


def task_payload(item: TaskRun) -> dict[str, Any]:
    return {
        "id": item.id,
        "task_name": item.task_name,
        "status": item.status,
        "payload": item.payload,
        "result": item.result,
        "error": item.error,
        "queued_at": item.queued_at,
        "started_at": item.started_at,
        "finished_at": item.finished_at,
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dict__"):
        return _json_safe(value.__dict__)
    return value


def _optional_str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return None


def _payload_str(payload: dict[str, Any], key: str, default: str) -> str:
    value = payload.get(key)
    if value in (None, ""):
        return default
    return str(value)


def _payload_int(payload: dict[str, Any], key: str, default: int) -> int:
    value = payload.get(key)
    if value in (None, ""):
        return default
    return int(value)


def _payload_float(payload: dict[str, Any], key: str, default: float) -> float:
    value = payload.get(key)
    if value in (None, ""):
        return default
    return float(value)


def _payload_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)
