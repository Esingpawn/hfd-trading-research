import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.tasks import enqueue_task, recent_tasks
from app.db import Base


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
