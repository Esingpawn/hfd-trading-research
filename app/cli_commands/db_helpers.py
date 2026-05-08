from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.models import CollectionRun
from app.services.collector import SnapshotCollector


async def latest_collection_run(session):
    rows = await session.execute(
        select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(1)
    )
    return rows.scalar_one_or_none()


async def collect_once(
    *,
    assets,
    timeframes,
    indicators,
    dry_run: bool,
):
    async with SessionLocal() as session:
        collector = SnapshotCollector(session)
        try:
            return await collector.collect(
                assets=assets,
                timeframes=timeframes,
                indicators=indicators,
                dry_run=dry_run,
            )
        finally:
            await collector.close()


def collection_result_payload(result) -> dict[str, object]:
    return {
        "status": result.status,
        "dry_run": result.dry_run,
        "assets": result.assets,
        "timeframes": result.timeframes,
        "indicators": result.indicators,
        "snapshots_written": result.snapshots_written,
        "prices_written": result.prices_written,
        "errors": result.errors,
    }
