from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import PriceSnapshot
from app.services.data_quality import data_quality_report


@pytest.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_data_quality_report_is_read_only_and_flags_invalid_prices(session) -> None:
    session.add(
        PriceSnapshot(
            symbol="BTCUSDT",
            price=0.0,
            raw_payload={},
            collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    await session.commit()

    report = await data_quality_report(session)

    assert report["status"] == "error"
    assert report["prices"]["invalid_price_count"] == 1
    assert report["policy"] == {
        "read_only": True,
        "opens_paper_trades": False,
        "opens_live_orders": False,
    }
    assert "invalid_prices" in {issue["code"] for issue in report["issues"]}
