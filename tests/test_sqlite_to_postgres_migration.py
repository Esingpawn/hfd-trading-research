from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import CollectionRun
from scripts.migrate_sqlite_to_postgres import migrate_table


@pytest.mark.asyncio
async def test_migrate_table_dry_run_counts_rows(tmp_path) -> None:
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'source.db'}"
    target_url = f"sqlite+aiosqlite:///{tmp_path / 'target.db'}"
    source_engine = create_async_engine(source_url)
    target_engine = create_async_engine(target_url)
    try:
        async with source_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with target_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        source_factory = async_sessionmaker(source_engine, expire_on_commit=False)
        target_factory = async_sessionmaker(target_engine, expire_on_commit=False)
        async with source_factory() as source:
            source.add(
                CollectionRun(
                    status="completed",
                    dry_run=False,
                    requested_assets=["BTC"],
                    requested_timeframes=["short"],
                    requested_indicators=["smart_money_cost"],
                    snapshots_written=1,
                    prices_written=1,
                    errors=[],
                    started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    finished_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                )
            )
            await source.commit()
        async with source_factory() as source, target_factory() as target:
            count = await migrate_table(
                source,
                target,
                CollectionRun,
                batch_size=1,
                dry_run=True,
            )
        assert count == 1
    finally:
        await source_engine.dispose()
        await target_engine.dispose()
