from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.application.tasks import enqueue_task, recent_tasks

router = APIRouter()


@router.get("/tasks")
async def list_tasks(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, object]]:
    return await recent_tasks(session, limit=limit)


@router.post("/tasks/enqueue")
async def enqueue_task_api(
    session: SessionDep,
    task_name: str = Query(..., min_length=1),
    coins: list[str] | None = Query(default=None),
    timeframes: list[str] | None = Query(default=None),
    indicators: list[str] | None = Query(default=None),
    dry_run: bool = Query(default=False),
    notify: bool = Query(default=False),
    limit: int | None = Query(default=None, ge=1),
    feature_limit: int | None = Query(default=None, ge=1),
    label_limit: int | None = Query(default=None, ge=1),
    signal_limit: int | None = Query(default=None, ge=1),
    report_limit: int | None = Query(default=None, ge=1),
    replay_limit: int | None = Query(default=None, ge=1),
    shadow_limit: int | None = Query(default=None, ge=1),
    scoreboard_limit: int | None = Query(default=None, ge=1),
    queued_after_seconds: int | None = Query(default=None, ge=1),
    running_after_seconds: int | None = Query(default=None, ge=1),
    horizons: list[str] | None = Query(default=None),
    horizon: str | None = Query(default=None, pattern="^(30m|1h|4h|24h)$"),
    min_samples: int | None = Query(default=None, ge=1),
    min_closed_trades: int | None = Query(default=None, ge=0),
    min_win_rate: float | None = Query(default=None, ge=0.0, le=1.0),
    min_profit_factor: float | None = Query(default=None, ge=0.0),
    min_avg_return: float | None = Query(default=None, ge=-1.0, le=1.0),
    segment_min_samples: int | None = Query(default=None, ge=1),
    min_segments: int | None = Query(default=None, ge=1),
    candidate_limit: int | None = Query(default=None, ge=1),
    include_watchlist: bool | None = Query(default=None),
    dedupe_research_samples: bool | None = Query(default=None),
    dedupe_bucket_minutes: int | None = Query(default=None, ge=1, le=1440),
    min_unique_time_buckets: int | None = Query(default=None, ge=1),
    min_unique_event_days: int | None = Query(default=None, ge=1),
    min_unique_market_windows: int | None = Query(default=None, ge=1),
    min_unique_collection_runs: int | None = Query(default=None, ge=1),
    market_window_hours: int | None = Query(default=None, ge=1, le=24),
    max_same_return_samples: int | None = Query(default=None, ge=1),
    max_return_cluster_ratio: float | None = Query(default=None, ge=0.0, le=1.0),
    confirmation_window_minutes: int | None = Query(default=None, ge=1, le=1440),
    max_candidate_age_hours: float | None = Query(default=None, ge=0.0),
    entry_tolerance_pct: float | None = Query(default=None, ge=0.0, le=0.2),
    materialize: bool | None = Query(default=None),
    mark_first: bool | None = Query(default=None),
    persist: bool | None = Query(default=None),
    refresh_labeled: bool = Query(default=False),
    force: bool | None = Query(default=None),
) -> dict[str, object]:
    payload = _task_enqueue_payload(
        coins=coins,
        timeframes=timeframes,
        indicators=indicators,
        dry_run=dry_run,
        notify=notify,
        limit=limit,
        feature_limit=feature_limit,
        label_limit=label_limit,
        signal_limit=signal_limit,
        report_limit=report_limit,
        replay_limit=replay_limit,
        shadow_limit=shadow_limit,
        scoreboard_limit=scoreboard_limit,
        queued_after_seconds=queued_after_seconds,
        running_after_seconds=running_after_seconds,
        horizons=horizons,
        horizon=horizon,
        min_samples=min_samples,
        min_closed_trades=min_closed_trades,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_avg_return=min_avg_return,
        segment_min_samples=segment_min_samples,
        min_segments=min_segments,
        candidate_limit=candidate_limit,
        include_watchlist=include_watchlist,
        dedupe_research_samples=dedupe_research_samples,
        dedupe_bucket_minutes=dedupe_bucket_minutes,
        min_unique_time_buckets=min_unique_time_buckets,
        min_unique_event_days=min_unique_event_days,
        min_unique_market_windows=min_unique_market_windows,
        min_unique_collection_runs=min_unique_collection_runs,
        market_window_hours=market_window_hours,
        max_same_return_samples=max_same_return_samples,
        max_return_cluster_ratio=max_return_cluster_ratio,
        confirmation_window_minutes=confirmation_window_minutes,
        max_candidate_age_hours=max_candidate_age_hours,
        entry_tolerance_pct=entry_tolerance_pct,
        materialize=materialize,
        mark_first=mark_first,
        persist=persist,
        refresh_labeled=refresh_labeled,
        force=force,
    )
    return await enqueue_task(session, task_name=task_name, payload=payload)


def _task_enqueue_payload(**values: Any) -> dict[str, Any]:
    keep_false = {"persist", "dedupe_research_samples", "force", "materialize", "mark_first"}
    return {
        key: value
        for key, value in values.items()
        if value not in (None, []) and (value is not False or key in keep_false)
    }
