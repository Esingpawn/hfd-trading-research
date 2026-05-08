from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db_indexes import SQLITE_INDEX_SPECS
from app.models import SignalSnapshot
from app.services.storage_health import ensure_performance_indexes, storage_health


@pytest.fixture()
async def session(tmp_path, monkeypatch):
    db_path = tmp_path / "storage-health.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_performance_indexes_creates_required_indexes(session) -> None:
    result = await ensure_performance_indexes(session)
    rows = await session.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
    existing = {row[0] for row in rows.all()}

    assert result["status"] == "ok"
    assert {spec.name for spec in SQLITE_INDEX_SPECS}.issubset(existing)


@pytest.mark.asyncio
async def test_storage_health_reports_counts_indexes_and_raw_payload(session) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        SignalSnapshot(
            symbol="BTCUSDT",
            asset_tier="core",
            timeframe="short",
            interval="30m",
            indicator="smart_money_cost",
            endpoint="/api/pro/pro_data",
            raw_payload={"payload": "x" * 128},
            summary_payload={"bias": "long"},
            collected_at=now,
        )
    )
    await session.commit()

    before_indexes = await storage_health(session)
    await ensure_performance_indexes(session)
    after_indexes = await storage_health(session)

    counts = {row["table"]: row["rows"] for row in after_indexes["tables"]}
    assert after_indexes["database_url_kind"] == "sqlite"
    assert counts["signal_snapshots"] == 1
    assert after_indexes["raw_payload"]["rows"] == 1
    assert after_indexes["raw_payload"]["raw_bytes"] >= 128
    assert before_indexes["indexes"]["missing_required"]
    assert after_indexes["indexes"]["missing_required"] == []
    assert after_indexes["sqlite"]["total_bytes"] > 0
    assert after_indexes["page_stats"]["db_bytes"] > 0
    assert after_indexes["recommendations"]
