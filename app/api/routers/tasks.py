from __future__ import annotations

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
    horizons: list[str] | None = Query(default=None),
    horizon: str | None = Query(default=None, pattern="^(30m|1h|4h|24h)$"),
    min_samples: int | None = Query(default=None, ge=1),
    min_win_rate: float | None = Query(default=None, ge=0.0, le=1.0),
    min_profit_factor: float | None = Query(default=None, ge=0.0),
    min_avg_return: float | None = Query(default=None, ge=-1.0, le=1.0),
    segment_min_samples: int | None = Query(default=None, ge=1),
    min_segments: int | None = Query(default=None, ge=1),
    candidate_limit: int | None = Query(default=None, ge=1),
    persist: bool | None = Query(default=None),
    refresh_labeled: bool = Query(default=False),
) -> dict[str, object]:
    payload = {
        key: value
        for key, value in {
            "coins": coins,
            "timeframes": timeframes,
            "indicators": indicators,
            "dry_run": dry_run,
            "notify": notify,
            "limit": limit,
            "horizons": horizons,
            "horizon": horizon,
            "min_samples": min_samples,
            "min_win_rate": min_win_rate,
            "min_profit_factor": min_profit_factor,
            "min_avg_return": min_avg_return,
            "segment_min_samples": segment_min_samples,
            "min_segments": min_segments,
            "candidate_limit": candidate_limit,
            "persist": persist,
            "refresh_labeled": refresh_labeled,
        }.items()
        if value not in (None, []) and (value is not False or key == "persist")
    }
    return await enqueue_task(session, task_name=task_name, payload=payload)
