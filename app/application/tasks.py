from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.queue import build_queue
from app.models import TaskRun


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
    return [
        {
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
        for item in rows.scalars()
    ]
