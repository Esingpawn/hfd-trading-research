from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings
from app.db_indexes import SQLITE_INDEX_SPECS


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return
    raw_path = database_url.removeprefix(prefix)
    if raw_path in (":memory:", ""):
        return
    Path(raw_path).parent.mkdir(parents=True, exist_ok=True)


def create_engine_from_settings() -> AsyncEngine:
    settings = get_settings()
    _ensure_sqlite_parent(settings.database_url)
    connect_args = {"timeout": 30} if settings.database_url.startswith("sqlite+") else {}
    created_engine = create_async_engine(
        settings.database_url,
        connect_args=connect_args,
        future=True,
    )
    if settings.database_url.startswith("sqlite+"):
        _configure_sqlite(created_engine)
    return created_engine


def _configure_sqlite(created_engine: AsyncEngine) -> None:
    @event.listens_for(created_engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


engine = create_engine_from_settings()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if get_settings().database_url.startswith("sqlite+"):
            for spec in SQLITE_INDEX_SPECS:
                await conn.exec_driver_sql(spec.create_sql)
