from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.queue import build_queue
from app.models import TaskRun
from app.services.data_quality import data_quality_report
from app.services.collector import SnapshotCollector
from app.services.feature_candidates import (
    feature_candidate_screen,
    feature_paper_ab,
    generate_default_research_reports,
    feature_segment_candidate_screen,
    feature_segment_paper_ab,
)
from app.services.features import (
    backfill_feature_events,
    backfill_feature_labels,
    reset_feature_research,
    refresh_feature_research,
)
from app.services.paper import mark_open_trades, paper_scan
from app.services.shadow_paper import (
    mark_shadow_paper_trades,
    shadow_paper_promotion_report,
    shadow_paper_replay,
    shadow_paper_replay_all,
    shadow_paper_scan,
)
from app.services.signal_attribution import backfill_signal_outcomes
from app.services.telegram import TelegramClient
from app.application.storage import run_storage_maintenance


TERMINAL_TASK_STATUSES = {"completed", "failed"}
ACTIVE_TASK_STATUSES = {"queued", "recorded", "running"}
DEFAULT_STALE_QUEUED_SECONDS = 30 * 60
DEFAULT_STALE_RUNNING_SECONDS = 60 * 60


async def enqueue_task(
    session: AsyncSession,
    *,
    task_name: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = TaskRun(task_name=task_name, payload=payload or {}, result={})
    session.add(item)
    await session.flush()
    await session.commit()
    queue_result = await build_queue().enqueue(task_name, {"task_run_id": item.id, **(payload or {})})
    item.result = {"queue": queue_result}
    if queue_result.get("status") == "queued":
        item.status = "queued"
    else:
        item.status = "recorded"
    await session.commit()
    return {"task_run_id": item.id, "status": item.status, "queue": queue_result}


async def recent_tasks(session: AsyncSession, *, limit: int = 50) -> list[dict[str, Any]]:
    await reap_stale_tasks(session)
    rows = await session.execute(select(TaskRun).order_by(TaskRun.queued_at.desc()).limit(limit))
    return [task_payload(item) for item in rows.scalars()]


async def reap_stale_tasks(
    session: AsyncSession,
    *,
    queued_after_seconds: int = DEFAULT_STALE_QUEUED_SECONDS,
    running_after_seconds: int = DEFAULT_STALE_RUNNING_SECONDS,
) -> dict[str, Any]:
    now = _utc_now()
    queued_cutoff = now - timedelta(seconds=max(1, queued_after_seconds))
    running_cutoff = now - timedelta(seconds=max(1, running_after_seconds))
    rows = await session.execute(
        select(TaskRun)
        .where(
            TaskRun.finished_at.is_(None),
            TaskRun.status.in_(ACTIVE_TASK_STATUSES),
            or_(
                and_(TaskRun.status.in_({"queued", "recorded"}), TaskRun.queued_at <= queued_cutoff),
                and_(
                    TaskRun.status == "running",
                    or_(
                        and_(TaskRun.started_at.is_not(None), TaskRun.started_at <= running_cutoff),
                        and_(TaskRun.started_at.is_(None), TaskRun.queued_at <= running_cutoff),
                    ),
                ),
            ),
        )
        .order_by(TaskRun.queued_at)
    )
    stale = list(rows.scalars())
    reaped: list[dict[str, Any]] = []
    for item in stale:
        previous_status = item.status
        age_anchor = item.started_at if previous_status == "running" and item.started_at else item.queued_at
        age_seconds = max(0.0, (now - _aware(age_anchor)).total_seconds())
        item.status = "failed"
        item.finished_at = now
        item.error = item.error or f"stale task reaped after {int(age_seconds)}s in {previous_status}"
        item.result = {
            **dict(item.result or {}),
            "stale_reaper": {
                "previous_status": previous_status,
                "age_seconds": round(age_seconds, 3),
                "reaped_at": now.isoformat(),
            },
        }
        reaped.append(
            {
                "id": item.id,
                "task_name": item.task_name,
                "previous_status": previous_status,
                "age_seconds": round(age_seconds, 3),
            }
        )
    if stale:
        await session.commit()
    return {
        "status": "ok",
        "reaped_count": len(reaped),
        "queued_after_seconds": max(1, queued_after_seconds),
        "running_after_seconds": max(1, running_after_seconds),
        "reaped": reaped,
    }


async def run_task_by_id(session: AsyncSession, task_run_id: str) -> dict[str, Any]:
    item = await session.get(TaskRun, task_run_id)
    if item is None:
        raise ValueError(f"task_run not found: {task_run_id}")
    if item.status in TERMINAL_TASK_STATUSES and item.finished_at is not None:
        return {**task_payload(item), "skipped": True, "skip_reason": "task_already_terminal"}
    return await run_task_record(session, item)


async def run_task_record(session: AsyncSession, item: TaskRun) -> dict[str, Any]:
    item_id = item.id
    task_name = item.task_name
    payload = dict(item.payload or {})
    initial_result = dict(item.result or {})
    queued_at = item.queued_at
    item.status = "running"
    started_at = _utc_now()
    item.started_at = started_at
    item.error = None
    await session.commit()
    try:
        result = await execute_task(session, task_name, payload)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        await session.rollback()
        stored = await session.get(TaskRun, item_id)
        if stored is None:
            raise
        stored.status = "failed"
        stored.error = error
        stored.finished_at = _utc_now()
        await session.commit()
        await _notify_task_failure(task_name=task_name, item_id=item_id, error=error)
        raise
    stored = await session.get(TaskRun, item_id)
    if stored is None:
        raise ValueError(f"task_run not found after execution: {item_id}")
    stored_result = _json_safe({**initial_result, "execution": result})
    finished_at = _utc_now()
    stored.status = "completed"
    stored.result = stored_result
    stored.finished_at = finished_at
    await session.commit()
    return {
        "id": item_id,
        "task_name": task_name,
        "status": "completed",
        "payload": payload,
        "result": stored_result,
        "error": None,
        "queued_at": queued_at,
        "started_at": started_at,
        "finished_at": finished_at,
    }


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
    if task_name in {"shadow_paper.scan", "shadow-paper-scan"}:
        return await shadow_paper_scan(
            session,
            candidate_limit=_payload_int(payload, "candidate_limit", 50),
            include_watchlist=_payload_bool(payload, "include_watchlist", True),
        )
    if task_name in {"shadow_paper.replay", "shadow-paper-replay"}:
        return await shadow_paper_replay(
            session,
            horizon=_payload_str(payload, "horizon", "30m"),
            limit=_payload_int(payload, "limit", 500),
            candidate_limit=_payload_int(payload, "candidate_limit", 50),
            include_watchlist=_payload_bool(payload, "include_watchlist", True),
        )
    if task_name in {"shadow_paper.replay_all", "shadow-paper-replay-all"}:
        return await shadow_paper_replay_all(
            session,
            horizons=_optional_str_list(payload.get("horizons")),
            limit=_payload_int(payload, "limit", 500),
            candidate_limit=_payload_int(payload, "candidate_limit", 50),
            include_watchlist=_payload_bool(payload, "include_watchlist", True),
        )
    if task_name in {"shadow_paper.mark", "shadow-paper-mark"}:
        return await mark_shadow_paper_trades(session)
    if task_name in {"shadow_paper.promotion", "shadow-paper-promotion"}:
        return await shadow_paper_promotion_report(session)
    if task_name in {"research.accelerate", "research-accelerate"}:
        return await _research_acceleration_cycle(session, payload)
    if task_name in {"tasks.reap_stale", "tasks-reap-stale"}:
        return await reap_stale_tasks(
            session,
            queued_after_seconds=_payload_int(payload, "queued_after_seconds", DEFAULT_STALE_QUEUED_SECONDS),
            running_after_seconds=_payload_int(payload, "running_after_seconds", DEFAULT_STALE_RUNNING_SECONDS),
        )
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
    if task_name in {"features.segment_candidates", "features-segment-candidates"}:
        return await feature_segment_candidate_screen(
            session,
            horizon=_payload_str(payload, "horizon", "30m"),
            min_samples=_payload_int(payload, "min_samples", 30),
            min_win_rate=_payload_float(payload, "min_win_rate", 0.52),
            min_profit_factor=_payload_float(payload, "min_profit_factor", 1.2),
            min_avg_return=_payload_float(payload, "min_avg_return", 0.0),
            dedupe_research_samples=_payload_bool(payload, "dedupe_research_samples", True),
            dedupe_bucket_minutes=_payload_int(payload, "dedupe_bucket_minutes", 30),
            min_unique_time_buckets=_payload_int(payload, "min_unique_time_buckets", 3),
            min_unique_event_days=_payload_int(payload, "min_unique_event_days", 2),
            min_unique_market_windows=_payload_int(payload, "min_unique_market_windows", 2),
            min_unique_collection_runs=_payload_int(payload, "min_unique_collection_runs", 2),
            market_window_hours=_payload_int(payload, "market_window_hours", 8),
            max_same_return_samples=_payload_int(payload, "max_same_return_samples", 10),
            max_return_cluster_ratio=_payload_float(payload, "max_return_cluster_ratio", 0.75),
            limit=_payload_int(payload, "limit", 20000),
            persist=_payload_bool(payload, "persist", True),
        )
    if task_name in {"features.segment_paper_ab", "features-segment-paper-ab"}:
        return await feature_segment_paper_ab(
            session,
            horizon=_payload_str(payload, "horizon", "30m"),
            min_samples=_payload_int(payload, "min_samples", 30),
            min_win_rate=_payload_float(payload, "min_win_rate", 0.52),
            min_profit_factor=_payload_float(payload, "min_profit_factor", 1.2),
            min_avg_return=_payload_float(payload, "min_avg_return", 0.0),
            dedupe_research_samples=_payload_bool(payload, "dedupe_research_samples", True),
            dedupe_bucket_minutes=_payload_int(payload, "dedupe_bucket_minutes", 30),
            min_unique_time_buckets=_payload_int(payload, "min_unique_time_buckets", 3),
            min_unique_event_days=_payload_int(payload, "min_unique_event_days", 2),
            min_unique_market_windows=_payload_int(payload, "min_unique_market_windows", 2),
            min_unique_collection_runs=_payload_int(payload, "min_unique_collection_runs", 2),
            market_window_hours=_payload_int(payload, "market_window_hours", 8),
            max_same_return_samples=_payload_int(payload, "max_same_return_samples", 10),
            max_return_cluster_ratio=_payload_float(payload, "max_return_cluster_ratio", 0.75),
            candidate_limit=_payload_int(payload, "candidate_limit", 50),
            limit=_payload_int(payload, "limit", 20000),
            persist=_payload_bool(payload, "persist", True),
        )
    if task_name in {"features.research_reports", "features-research-reports"}:
        return await generate_default_research_reports(
            session,
            horizon=_payload_str(payload, "horizon", "30m"),
            min_samples=_payload_int(payload, "min_samples", 30),
            limit=_payload_int(payload, "limit", 5000),
            max_age_seconds=0 if _payload_bool(payload, "force", False) else _payload_int(payload, "max_age_seconds", 3600),
        )
    if task_name in {"data_quality.report", "data-quality-report"}:
        return await data_quality_report(session)
    if task_name in {"storage.maintain", "storage-maintain"}:
        return await run_storage_maintenance(
            session,
            indexes=bool(payload.get("indexes", False)),
            checkpoint=bool(payload.get("checkpoint", False)),
            passive_checkpoint=bool(payload.get("passive_checkpoint", False)),
            optimize=bool(payload.get("optimize", False)),
        )
    raise ValueError(f"unsupported task_name: {task_name}")


async def _research_acceleration_cycle(session: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    horizon = _payload_str(payload, "horizon", "30m")
    feature_limit = _payload_int(payload, "feature_limit", _payload_int(payload, "limit", 500))
    label_limit = _payload_int(payload, "label_limit", 1000)
    report_limit = _payload_int(payload, "report_limit", 5000)
    candidate_limit = _payload_int(payload, "candidate_limit", 50)
    replay_limit = _payload_int(payload, "replay_limit", 500)
    result: dict[str, Any] = {
        "policy": {
            "opens_live_orders": False,
            "opens_paper_trades": False,
            "uses_shadow_paper_only": True,
            "purpose": "increase research sample coverage and promotion evidence",
        },
        "steps": {},
    }
    feature_events = await backfill_feature_events(
        session,
        limit=feature_limit,
        indicators=_optional_str_list(payload.get("indicators")),
    )
    result["steps"]["feature_events"] = feature_events.__dict__
    feature_labels = await backfill_feature_labels(
        session,
        limit=label_limit,
        horizons=_optional_str_list(payload.get("horizons")) or [horizon],
        refresh_labeled=_payload_bool(payload, "refresh_labeled", False),
    )
    result["steps"]["feature_labels"] = feature_labels.__dict__
    signal_labels = await backfill_signal_outcomes(session, limit=_payload_int(payload, "signal_limit", 1000))
    result["steps"]["signal_labels"] = signal_labels.__dict__
    result["steps"]["research_reports"] = await generate_default_research_reports(
        session,
        horizon=horizon,
        min_samples=_payload_int(payload, "min_samples", 30),
        limit=report_limit,
        max_age_seconds=0,
    )
    result["steps"]["shadow_mark"] = await mark_shadow_paper_trades(session)
    result["steps"]["shadow_replay"] = await shadow_paper_replay(
        session,
        horizon=horizon,
        limit=replay_limit,
        candidate_limit=candidate_limit,
        include_watchlist=_payload_bool(payload, "include_watchlist", True),
    )
    result["steps"]["shadow_scan"] = await shadow_paper_scan(
        session,
        candidate_limit=candidate_limit,
        include_watchlist=_payload_bool(payload, "include_watchlist", True),
    )
    result["steps"]["shadow_promotion"] = await shadow_paper_promotion_report(session)
    return result


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


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _notify_task_failure(*, task_name: str, item_id: str, error: str) -> None:
    client = TelegramClient()
    if not client.configured or not client.chat_id:
        return
    try:
        await client.send_message(
            "HFD task failed\n"
            f"task: {task_name}\n"
            f"id: {item_id}\n"
            f"error: {error[:500]}"
        )
    except Exception:
        return


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
