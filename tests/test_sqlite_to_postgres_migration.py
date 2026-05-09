from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.infrastructure.raw_store import LocalRawPayloadStore
from app.models import CollectionRun, SignalSnapshot
from scripts.migrate_sqlite_to_postgres import _column_names, _table_names, migrate_table


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


@pytest.mark.asyncio
async def test_table_names_supports_missing_new_tables(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE collection_runs (id TEXT PRIMARY KEY)")

        tables = await _table_names(engine)

        assert "collection_runs" in tables
        assert "trade_orders" not in tables
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migrate_signal_snapshots_can_externalize_raw_payloads(tmp_path) -> None:
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
        collected_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        async with source_factory() as source:
            source.add(
                SignalSnapshot(
                    id="snapshot-1",
                    symbol="BTCUSDT",
                    asset_tier="major",
                    timeframe="short",
                    interval="30m",
                    indicator="smart_money_cost",
                    endpoint="/api/pro/pro_data",
                    raw_payload={"klines": [[1, 2, 3, 4, 5]], "smart_money_cost": [1, 2]},
                    raw_payload_uri=None,
                    raw_payload_sha256=None,
                    raw_payload_bytes=None,
                    raw_payload_compression=None,
                    summary_payload={"kline_count": 1},
                    collected_at=collected_at,
                    created_at=collected_at,
                )
            )
            await source.commit()

        store = LocalRawPayloadStore(tmp_path / "raw_payloads")
        async with source_factory() as source, target_factory() as target:
            count = await migrate_table(
                source,
                target,
                SignalSnapshot,
                batch_size=1,
                dry_run=False,
                raw_store=store,
            )

        async with target_factory() as target:
            migrated = await target.get(SignalSnapshot, "snapshot-1")
        assert count == 1
        assert migrated is not None
        assert migrated.raw_payload == {}
        assert migrated.raw_payload_uri is not None
        assert store.resolve(migrated.raw_payload_uri).exists()
        assert migrated.raw_payload_compression == "gzip"
    finally:
        await source_engine.dispose()
        await target_engine.dispose()


@pytest.mark.asyncio
async def test_migrate_signal_snapshots_supports_legacy_source_columns(tmp_path) -> None:
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    target_url = f"sqlite+aiosqlite:///{tmp_path / 'target.db'}"
    source_engine = create_async_engine(source_url)
    target_engine = create_async_engine(target_url)
    try:
        async with source_engine.begin() as conn:
            await conn.exec_driver_sql(
                """
                CREATE TABLE signal_snapshots (
                    id VARCHAR(36) PRIMARY KEY,
                    symbol VARCHAR(24),
                    asset_tier VARCHAR(32),
                    timeframe VARCHAR(16),
                    interval VARCHAR(8),
                    indicator VARCHAR(64),
                    endpoint TEXT,
                    raw_payload JSON,
                    summary_payload JSON,
                    collected_at DATETIME,
                    created_at DATETIME
                )
                """
            )
            await conn.exec_driver_sql(
                """
                INSERT INTO signal_snapshots (
                    id, symbol, asset_tier, timeframe, interval, indicator, endpoint,
                    raw_payload, summary_payload, collected_at, created_at
                ) VALUES (
                    'legacy-1', 'BTCUSDT', 'major', 'short', '30m', 'smart_money_cost',
                    '/api/pro/pro_data', '{"payload":"value"}', '{"kline_count":1}',
                    '2026-01-01 00:00:00', '2026-01-01 00:00:00'
                )
                """
            )
        async with target_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        source_factory = async_sessionmaker(source_engine, expire_on_commit=False)
        target_factory = async_sessionmaker(target_engine, expire_on_commit=False)

        store = LocalRawPayloadStore(tmp_path / "raw_payloads")
        async with source_factory() as source, target_factory() as target:
            count = await migrate_table(
                source,
                target,
                SignalSnapshot,
                batch_size=1,
                dry_run=False,
                raw_store=store,
                source_columns=await _column_names(source_engine, "signal_snapshots"),
            )

        async with target_factory() as target:
            migrated = await target.get(SignalSnapshot, "legacy-1")
        assert count == 1
        assert migrated is not None
        assert migrated.raw_payload == {}
        assert migrated.raw_payload_uri is not None
        assert store.resolve(migrated.raw_payload_uri).exists()
    finally:
        await source_engine.dispose()
        await target_engine.dispose()
