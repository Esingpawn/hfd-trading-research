import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.task_worker import run_task_message_once
from app.db import Base
from app.models import TaskRun


@pytest.mark.asyncio
async def test_run_task_message_once_executes_task(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            item = TaskRun(task_name="storage.maintain", payload={"indexes": True}, result={})
            session.add(item)
            await session.commit()
            task_run_id = item.id

        result = await run_task_message_once(
            {"task_run_id": task_run_id},
            session_factory=session_factory,
        )

        assert result["status"] == "completed"
        assert result["task_name"] == "storage.maintain"
    finally:
        await engine.dispose()
