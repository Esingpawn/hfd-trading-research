from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import CollectionRun, PriceSnapshot, SignalSnapshot
from app.services.collector import SnapshotCollector


class FakeHfdClient:
    def __init__(self) -> None:
        self.flush_count_during_fetch = 0
        self.session = None

    async def close(self) -> None:
        return None

    async def fetch_price(self, coin: str) -> dict[str, object]:
        self.flush_count_during_fetch = int(getattr(self.session, "flush_count", 0))
        return {"price": "100.0", "symbol": f"{coin}USDT"}

    async def fetch_pro_data(self, coin: str, interval: str, indicator: str) -> dict[str, object]:
        self.flush_count_during_fetch = max(
            self.flush_count_during_fetch,
            int(getattr(self.session, "flush_count", 0)),
        )
        return {
            "klines": [[1_700_000_000_000, 100, 101, 99, 100, 10]],
            indicator: [{"timestamp": 1_700_000_000_000, "price": 100.0, "type": "CHoCH_Bullish"}],
        }


class BadPriceClient(FakeHfdClient):
    async def fetch_price(self, coin: str) -> dict[str, object]:
        return {"price": "not-a-number", "symbol": f"{coin}USDT"}


class FlushCountingSession:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.flush_count = 0

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def add(self, item) -> None:
        self.inner.add(item)

    async def flush(self, *args, **kwargs):
        self.flush_count += 1
        return await self.inner.flush(*args, **kwargs)


@pytest.mark.asyncio
async def test_collector_does_not_open_db_transaction_before_fetches() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as inner_session:
            session = FlushCountingSession(inner_session)
            client = FakeHfdClient()
            client.session = session
            collector = SnapshotCollector(session, client=client)

            result = await collector.collect(
                assets=["BTC"],
                timeframes=["short"],
                indicators=["inst_choch"],
            )

            runs = await inner_session.execute(select(CollectionRun))
            prices = await inner_session.execute(select(PriceSnapshot))
            snapshots = await inner_session.execute(select(SignalSnapshot))

        assert result.status == "completed"
        assert result.prices_written == 1
        assert result.snapshots_written == 1
        assert client.flush_count_during_fetch == 0
        assert len(runs.scalars().all()) == 1
        assert len(prices.scalars().all()) == 1
        assert len(snapshots.scalars().all()) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_collector_records_store_errors_in_collection_run() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            collector = SnapshotCollector(session, client=BadPriceClient())

            result = await collector.collect(
                assets=["BTC"],
                timeframes=["short"],
                indicators=["inst_choch"],
            )

            runs = await session.execute(select(CollectionRun))
            prices = await session.execute(select(PriceSnapshot))
            snapshots = await session.execute(select(SignalSnapshot))

        run = runs.scalar_one()
        assert result.status == "completed_with_errors"
        assert result.prices_written == 0
        assert result.snapshots_written == 1
        assert result.errors[0]["stage"] == "price_store"
        assert run.status == "completed_with_errors"
        assert run.errors[0]["stage"] == "price_store"
        assert prices.scalars().all() == []
        assert len(snapshots.scalars().all()) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_collector_externalizes_raw_payloads_before_db_write(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXTERNALIZE_RAW_PAYLOADS", "true")
    monkeypatch.setenv("RAW_PAYLOAD_DIR", str(tmp_path / "raw"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            collector = SnapshotCollector(session, client=FakeHfdClient())

            result = await collector.collect(
                assets=["BTC"],
                timeframes=["short"],
                indicators=["inst_choch"],
            )

            snapshots = await session.execute(select(SignalSnapshot))

        stored = snapshots.scalar_one()
        raw_path = collector.raw_store.resolve(stored.raw_payload_uri)
        assert result.status == "completed"
        assert stored.raw_payload == {}
        assert stored.raw_payload_uri is not None
        assert stored.raw_payload_sha256 is not None
        assert stored.summary_payload["kline_count"] == 1
        assert raw_path.exists()
    finally:
        await engine.dispose()
