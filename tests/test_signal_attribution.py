from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import PriceSnapshot, StrategyDecision
from app.services.signal_attribution import (
    backfill_signal_outcomes,
    record_signal_observations,
    signal_effectiveness,
)


@pytest.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


def decision(created_at: datetime) -> StrategyDecision:
    return StrategyDecision(
        strategy_name="multi_timeframe_cost_stack",
        strategy_version="0.2",
        symbol="BTCUSDT",
        asset_tier="core",
        direction="long",
        score=85.0,
        decision="open",
        reason={"rules": ["long_term_direction"], "states": [{"bias": "long"}]},
        risk_payload={
            "price_at_signal": 100.0,
            "modules": [
                {"name": "长期方向", "points": 25, "detail": "长期偏多", "status": "ok"},
                {"name": "订单流确认", "points": 10, "detail": "订单流存在", "status": "ok"},
            ],
            "execution_gate": {"ready": True},
            "opportunity_stage": {"key": "paper_ready"},
        },
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_record_signal_observations_creates_one_row_per_module(session) -> None:
    item = decision(datetime(2026, 1, 1, tzinfo=timezone.utc))
    session.add(item)
    await session.flush()

    rows = await record_signal_observations(session, item)

    assert len(rows) == 2
    assert rows[0].signal_name == "长期方向"
    assert rows[0].signal_role == "direction"
    assert rows[1].score_before == 25
    assert rows[1].score_after == 35


@pytest.mark.asyncio
async def test_backfill_signal_outcomes_labels_future_returns(session) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    item = decision(observed_at)
    session.add(item)
    await session.flush()
    await record_signal_observations(session, item)
    for minutes, price in [(0, 100.0), (30, 101.0), (60, 102.0), (240, 104.0), (1440, 108.0)]:
        session.add(
            PriceSnapshot(
                symbol="BTCUSDT",
                price=price,
                raw_payload={},
                collected_at=observed_at + timedelta(minutes=minutes),
            )
        )
    await session.commit()

    result = await backfill_signal_outcomes(session)
    report = await signal_effectiveness(session, horizon="4h")

    assert result.updated == 2
    assert result.imported == 0
    assert report["labeled_count"] == 2
    assert report["signals"][0]["sample_count"] == 1
    assert report["signals"][0]["avg_return"] == pytest.approx(0.04)
    assert report["roles"][0]["sample_count"] >= 1


@pytest.mark.asyncio
async def test_backfill_keeps_pending_when_future_price_is_missing(session) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    item = decision(observed_at)
    session.add(item)
    await session.flush()
    await record_signal_observations(session, item)

    result = await backfill_signal_outcomes(session)

    assert result.pending == 2
    assert result.updated == 0


@pytest.mark.asyncio
async def test_backfill_imports_existing_strategy_decisions(session) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(decision(observed_at))
    await session.commit()

    result = await backfill_signal_outcomes(session)
    report = await signal_effectiveness(session)

    assert result.imported == 2
    assert report["sample_count"] == 2
