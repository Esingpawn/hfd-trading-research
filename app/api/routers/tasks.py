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
) -> dict[str, object]:
    return await enqueue_task(session, task_name=task_name, payload={})
