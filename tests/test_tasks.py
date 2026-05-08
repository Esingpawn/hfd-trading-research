import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.tasks import enqueue_task, recent_tasks, run_task_by_id
from app.db import Base
from app.models import TaskRun


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
