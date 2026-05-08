from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.storage_health import (
    ensure_performance_indexes,
    sqlite_checkpoint,
    sqlite_optimize,
    storage_health,
)


async def get_storage_health(session: AsyncSession) -> dict[str, Any]:
    return await storage_health(session)


async def run_storage_maintenance(
    session: AsyncSession,
    *,
    indexes: bool = False,
    checkpoint: bool = False,
    passive_checkpoint: bool = False,
    optimize: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {"actions": {}}
    if indexes:
        result["actions"]["indexes"] = await ensure_performance_indexes(session)
    if checkpoint:
        result["actions"]["checkpoint"] = await sqlite_checkpoint(
            session,
            truncate=not passive_checkpoint,
        )
    if optimize:
        result["actions"]["optimize"] = await sqlite_optimize(session)
    result["storage"] = await storage_health(session)
    return result
