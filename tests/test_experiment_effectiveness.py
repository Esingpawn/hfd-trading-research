from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import SignalSnapshot
from app.services.experiment_effectiveness import experiment_feature_effectiveness


@pytest.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


def klines(count: int = 16) -> list[list[float]]:
    start = 1_700_000_000_000
    rows = []
    for index in range(count):
        price = 100 + index
        rows.append([start + index * 1_800_000, price, price + 0.5, price - 0.5, price + 1, 10])
    return rows


@pytest.mark.asyncio
async def test_experiment_feature_effectiveness_scores_latest_snapshots(session) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = klines()
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
                "inst_choch": [
                    {"timestamp": rows[1][0], "price": rows[1][2], "type": "CHoCH_Bullish"},
                    {"timestamp": rows[2][0], "price": rows[2][2], "type": "BOS_Bullish"},
                    {"timestamp": rows[3][0], "price": rows[3][2], "type": "CHoCH_Bullish"},
                    {"timestamp": rows[4][0], "price": rows[4][2], "type": "BOS_Bullish"},
                    {"timestamp": rows[5][0], "price": rows[5][2], "type": "CHoCH_Bullish"},
                ],
            },
            summary_payload={},
            collected_at=now,
        )
    )
    await session.commit()

    report = await experiment_feature_effectiveness(session, horizon="30m", min_samples=3)

    rows_by_key = {row["key"]: row for row in report["indicators"]}
    assert report["policy"]["used_for_opening_decisions"] is False
    assert rows_by_key["inst_choch"]["sample_count"] == 5
    assert rows_by_key["inst_choch"]["status"] == "candidate"
    assert rows_by_key["inst_choch"]["avg_return"] > 0
    assert rows_by_key["inst_choch"]["used_for_execution_weights"] is False


@pytest.mark.asyncio
async def test_experiment_feature_effectiveness_marks_empty_indicators_insufficient(session) -> None:
    report = await experiment_feature_effectiveness(session, horizon="4h", min_samples=5)

    rows_by_key = {row["key"]: row for row in report["indicators"]}
    assert report["event_count"] == 0
    assert rows_by_key["fair_value_gap"]["status"] == "insufficient"
