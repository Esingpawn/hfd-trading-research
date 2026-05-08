from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import BacktestRun, SignalObservation, SignalSnapshot
from app.services.indicator_catalog import indicator_experiment_coverage


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
async def test_indicator_experiment_coverage_separates_live_weights_and_backtest(session) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        SignalSnapshot(
            symbol="BTCUSDT",
            asset_tier="core",
            timeframe="short",
            interval="30m",
            indicator="smart_money_cost",
            endpoint="/api/pro/pro_data",
            raw_payload={"smart_money_cost": []},
            summary_payload={},
            collected_at=now,
        )
    )
    session.add(
        SignalSnapshot(
            symbol="BTCUSDT",
            asset_tier="core",
            timeframe="short",
            interval="30m",
            indicator="fair_value_gap",
            endpoint="/api/pro/pro_data",
            raw_payload={"fair_value_gap": []},
            summary_payload={},
            collected_at=now,
        )
    )
    session.add(
        SignalObservation(
            strategy_decision_id="decision-1",
            symbol="BTCUSDT",
            asset_tier="core",
            signal_name="长期方向",
            signal_role="direction",
            direction="long",
            strength=25,
            price_at_signal=100,
            strategy_decision="open",
            strategy_score=80,
            labels={"return_4h": 0.02},
            status="labeled",
            observed_at=now,
        )
    )
    session.add(
        BacktestRun(
            strategy="cost_band_retest_static_v0",
            status="completed",
            requested_assets=["BTC"],
            requested_timeframes=["short"],
            params={},
            results=[{"coin": "BTC", "trade_count": 12}],
            errors=[],
        )
    )
    await session.commit()

    report = await indicator_experiment_coverage(session)

    sources = {row["source"]: row for row in report["weight_sources"]}
    catalog = {row["key"]: row for row in report["indicator_catalog"]}
    matrix = {row["key"]: row for row in report["experiment_matrix"]}
    assert sources["paper_signal_attribution"]["used_for_execution_weights"] is True
    assert sources["historical_backtest"]["used_for_execution_weights"] is False
    assert report["experiment_policy"]["used_for_execution_weights"] is False
    assert report["experiment_policy"]["used_for_opening_decisions"] is False
    assert catalog["smart_money_cost"]["used_in_backtest"] is True
    assert catalog["smart_money_cost"]["live_labeled_count"] == 1
    assert catalog["fair_value_gap"]["status"] == "experiment"
    assert catalog["fair_value_gap"]["selected_for_experiment"] is True
    assert catalog["fair_value_gap"]["used_for_execution_weights"] is False
    assert matrix["fair_value_gap"]["experiment_status"] == "collecting"
    assert matrix["fair_value_gap"]["snapshot_count"] == 1
    assert matrix["fair_value_gap"]["coverage_slots"] == 1
    assert matrix["fair_value_gap"]["expected_coverage_slots"] == 27
    assert matrix["fair_value_gap"]["used_for_opening_decisions"] is False
