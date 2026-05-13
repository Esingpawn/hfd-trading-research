import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application import tasks
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


@pytest.mark.asyncio
async def test_task_failure_notification_is_best_effort(tmp_path, monkeypatch) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    sent: list[str] = []

    class FakeTelegramClient:
        configured = True
        chat_id = "123"

        async def send_message(self, text: str, chat_id: str | None = None) -> dict[str, object]:
            sent.append(text)
            return {"message_id": 1}

    monkeypatch.setattr(tasks, "TelegramClient", FakeTelegramClient)
    try:
        async with session_factory() as session:
            item = TaskRun(task_name="unknown.task", payload={}, result={})
            session.add(item)
            await session.commit()
            task_run_id = item.id

        with pytest.raises(ValueError):
            await run_task_message_once(
                {"task_run_id": task_run_id},
                session_factory=session_factory,
            )

        assert sent
        assert "unknown.task" in sent[0]
    finally:
        await engine.dispose()
