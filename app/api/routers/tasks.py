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
    min_samples: int | None = Query(default=None, ge=1),
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
            "min_samples": min_samples,
        }.items()
        if value not in (None, [], False)
    }
    return await enqueue_task(session, task_name=task_name, payload=payload)
