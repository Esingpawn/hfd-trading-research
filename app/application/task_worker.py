from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.tasks import run_task_by_id
from app.db import SessionLocal
from app.infrastructure.queue import build_queue


@dataclass(frozen=True)
class WorkerResult:
    processed: int
    failed: int
    idle: int


async def run_task_worker(
    *,
    max_tasks: int = 0,
    idle_sleep_seconds: float = 1.0,
    dequeue_timeout_seconds: int = 5,
    session_factory: async_sessionmaker = SessionLocal,
) -> WorkerResult:
    queue = build_queue()
    processed = 0
    failed = 0
    idle = 0
    while True:
        message = await queue.dequeue(timeout_seconds=dequeue_timeout_seconds)
        if message is None:
            idle += 1
            if max_tasks and processed + failed >= max_tasks:
                break
            await asyncio.sleep(idle_sleep_seconds)
            continue
        task_run_id = message.payload.get("task_run_id")
        if not isinstance(task_run_id, str) or not task_run_id:
            failed += 1
            print({"status": "failed", "reason": "missing_task_run_id", "message": message.raw}, flush=True)
            if max_tasks and processed + failed >= max_tasks:
                break
            continue
        try:
            async with session_factory() as session:
                result = await run_task_by_id(session, task_run_id)
            processed += 1
            print({"status": "completed", "task_run_id": task_run_id, "task_name": result["task_name"]}, flush=True)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print({"status": "failed", "task_run_id": task_run_id, "error": str(exc)}, flush=True)
        if max_tasks and processed + failed >= max_tasks:
            break
    return WorkerResult(processed=processed, failed=failed, idle=idle)


async def run_task_message_once(
    payload: dict[str, Any],
    *,
    session_factory: async_sessionmaker = SessionLocal,
) -> dict[str, Any]:
    task_run_id = payload.get("task_run_id")
    if not isinstance(task_run_id, str) or not task_run_id:
        raise ValueError("missing task_run_id")
    async with session_factory() as session:
        return await run_task_by_id(session, task_run_id)
