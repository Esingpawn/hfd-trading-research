from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import ExperimentRun, FeatureEvent, FeatureLabel, PaperTrade
from app.services.darkflow_playbooks import darkflow_playbook_backtest, latest_darkflow_playbook_backtest


@pytest.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def add_labeled_event(
    session,
    *,
    indicator: str,
    subtype: str,
    event_ts: datetime,
    return_pct: float,
    symbol: str = "BTCUSDT",
    timeframe: str = "short",
    direction: str = "long",
) -> FeatureEvent:
    suffix = f"{indicator}-{subtype}-{symbol}-{timeframe}-{event_ts.timestamp()}-{direction}"
    event = FeatureEvent(
        snapshot_id=f"snapshot-{suffix}",
        symbol=symbol,
        asset_tier="core",
        timeframe=timeframe,
        interval="30m",
        indicator=indicator,
        event_key=f"event-{suffix}",
        feature_name=indicator,
        direction=direction,
        event_ts=event_ts,
        event_price=100.0,
        strength=0.8,
        subtype=subtype,
        source_payload_key=indicator,
        context={},
    )
    session.add(event)
    await session.flush()
    session.add(
        FeatureLabel(
            feature_event_id=event.id,
            horizon="4h",
            return_pct=return_pct,
            mfe=max(return_pct, 0.0) + 0.002,
            mae=min(return_pct, 0.0) - 0.001,
            future_price=100.0 * (1 + return_pct),
            future_at=event_ts + timedelta(hours=4),
            status="labeled",
        )
    )
    return event


@pytest.mark.asyncio
async def test_darkflow_playbook_backtest_groups_official_tutorial_semantics(session) -> None:
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, value in enumerate([0.012, 0.01, 0.009, 0.011, 0.008, -0.002]):
        event_ts = base_ts + timedelta(minutes=index * 30)
        await add_labeled_event(session, indicator="trend_price", subtype="support", event_ts=event_ts, return_pct=value)
        await add_labeled_event(
            session,
            indicator="imbalance",
            subtype="confirm",
            event_ts=event_ts + timedelta(minutes=10),
            return_pct=value / 2,
        )
    await add_labeled_event(
        session,
        indicator="liquidity_sweep",
        subtype="bottom_sweep",
        event_ts=base_ts + timedelta(hours=6),
        return_pct=0.02,
    )
    await session.commit()

    report = await darkflow_playbook_backtest(
        session,
        horizon="4h",
        min_samples=6,
        min_win_rate=0.6,
        min_profit_factor=1.1,
        limit=100,
    )
    pullback = next(item for item in report["playbooks"] if item["key"] == "pullback_to_cost")

    assert report["policy"]["opens_paper_trades"] is False
    assert report["covered_labeled_count"] >= 7
    assert pullback["sample_count"] == 6
    assert pullback["confirmed_sample_count"] == 6
    assert pullback["readiness"]["status"] == "candidate"
    assert pullback["stats"]["profit_factor"] > 1.1


@pytest.mark.asyncio
async def test_darkflow_playbook_backtest_persists_latest_report_without_paper_trades(session) -> None:
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(3):
        await add_labeled_event(
            session,
            indicator="liquidity_sweep",
            subtype="bottom_sweep",
            event_ts=base_ts + timedelta(hours=index),
            return_pct=0.01,
        )
    await session.commit()

    report = await darkflow_playbook_backtest(session, horizon="4h", min_samples=6, limit=100, persist=True)
    latest = await latest_darkflow_playbook_backtest(session, horizon="4h")
    paper_count = await session.scalar(select(func.count()).select_from(PaperTrade))
    experiment = await session.scalar(select(ExperimentRun).where(ExperimentRun.name == "darkflow_playbook_backtest_4h"))

    assert report["experiment_run"]["status"] == "research"
    assert latest["materialized"] is True
    assert latest["source_experiment_run_id"] == experiment.id
    assert latest["policy"]["opens_live_orders"] is False
    assert paper_count == 0
