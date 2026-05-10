from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.tasks import enqueue_task, recent_tasks, run_task_by_id
from app.db import Base
from app.models import FeatureEvent, SignalSnapshot, TaskRun


@pytest.fixture()
async def session(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_enqueue_task_records_task_without_redis(session) -> None:
    result = await enqueue_task(session, task_name="collect", payload={"coin": "BTC"})
    rows = await recent_tasks(session)

    assert result["status"] == "recorded"
    assert rows[0]["task_name"] == "collect"
    assert rows[0]["payload"] == {"coin": "BTC"}


@pytest.mark.asyncio
async def test_run_task_by_id_executes_storage_maintenance(session) -> None:
    item = TaskRun(task_name="storage.maintain", payload={"indexes": True}, result={})
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)
    rows = await session.execute(select(TaskRun).where(TaskRun.id == item.id))
    stored = rows.scalar_one()

    assert result["status"] == "completed"
    assert stored.status == "completed"
    assert stored.finished_at is not None
    assert stored.result["execution"]["actions"]["indexes"]["status"] == "ok"


@pytest.mark.asyncio
async def test_run_task_by_id_executes_feature_backfill(session) -> None:
    rows = [
        [1_700_000_000_000, 100, 101, 99, 102, 10],
        [1_700_001_800_000, 101, 102, 100, 103, 10],
    ]
    session.add(
        SignalSnapshot(
            symbol="BTCUSDT",
            asset_tier="core",
            timeframe="short",
            interval="30m",
            indicator="inst_choch",
            endpoint="/api/pro/pro_data",
            raw_payload={
                "klines": rows,
                "inst_choch": [{"timestamp": rows[0][0], "price": 101, "type": "CHoCH_Bullish"}],
            },
            summary_payload={},
            collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    item = TaskRun(task_name="features.backfill", payload={"limit": 10}, result={})
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)
    stored_events = await session.execute(select(FeatureEvent))

    assert result["status"] == "completed"
    assert result["result"]["execution"]["events_inserted"] == 1
    assert len(stored_events.scalars().all()) == 1


@pytest.mark.asyncio
async def test_run_task_by_id_executes_feature_reset(session) -> None:
    rows = [
        [1_700_000_000_000, 100, 101, 99, 102, 10],
        [1_700_001_800_000, 101, 102, 100, 103, 10],
    ]
    session.add(
        SignalSnapshot(
            symbol="BTCUSDT",
            asset_tier="core",
            timeframe="short",
            interval="30m",
            indicator="inst_choch",
            endpoint="/api/pro/pro_data",
            raw_payload={
                "klines": rows,
                "inst_choch": [{"timestamp": rows[0][0], "price": 101, "type": "CHoCH_Bullish"}],
            },
            summary_payload={},
            collected_at=datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc),
        )
    )
    backfill = TaskRun(task_name="features.backfill", payload={"limit": 10}, result={})
    session.add(backfill)
    await session.commit()
    await run_task_by_id(session, backfill.id)

    reset = TaskRun(task_name="features.reset", payload={}, result={})
    session.add(reset)
    await session.commit()

    result = await run_task_by_id(session, reset.id)
    stored_events = await session.execute(select(FeatureEvent))

    assert result["status"] == "completed"
    assert result["result"]["execution"]["events_deleted"] == 1
    assert stored_events.scalars().all() == []


@pytest.mark.asyncio
async def test_run_task_by_id_records_failure(session) -> None:
    item = TaskRun(task_name="unknown.task", payload={}, result={})
    session.add(item)
    await session.commit()

    with pytest.raises(ValueError):
        await run_task_by_id(session, item.id)

    rows = await session.execute(select(TaskRun).where(TaskRun.id == item.id))
    stored = rows.scalar_one()
    assert stored.status == "failed"
    assert "unsupported task_name" in str(stored.error)
