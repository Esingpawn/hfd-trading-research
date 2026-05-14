from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import CollectionRun, ExperimentRun, FeatureEvent, FeatureLabel, PaperTrade, SignalSnapshot
from app.services.feature_candidates import (
    feature_candidate_screen,
    feature_paper_ab,
    feature_segment_candidate_screen,
    feature_segment_paper_ab,
    generate_default_research_reports,
    latest_feature_candidate_screen,
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


async def add_labeled_feature(
    session,
    *,
    feature_name: str,
    subtype: str,
    direction: str = "long",
    returns: list[float],
    symbol: str = "BTCUSDT",
    timeframe: str = "short",
    event_spacing_minutes: int = 31,
    start_ts: datetime | None = None,
    create_snapshots: bool = False,
    collection_run_ids: list[str] | None = None,
) -> None:
    base_ts = start_ts or datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, return_pct in enumerate(returns):
        event_ts = base_ts + timedelta(minutes=index * event_spacing_minutes)
        key_suffix = f"{event_ts.strftime('%Y%m%d%H%M')}-{index}"
        snapshot_id = f"snapshot-{feature_name}-{subtype}-{symbol}-{timeframe}-{key_suffix}"
        if create_snapshots:
            session.add(
                SignalSnapshot(
                    id=snapshot_id,
                    collection_run_id=(collection_run_ids or [None])[index % len(collection_run_ids or [None])],
                    symbol=symbol,
                    asset_tier="core",
                    timeframe=timeframe,
                    interval="30m",
                    indicator=feature_name.split(".")[0],
                    endpoint="/api/pro/pro_data",
                    raw_payload={},
                    raw_payload_uri=None,
                    raw_payload_sha256=None,
                    raw_payload_bytes=None,
                    raw_payload_compression=None,
                    summary_payload={},
                    collected_at=event_ts,
                    created_at=event_ts,
                )
            )
        event = FeatureEvent(
            snapshot_id=snapshot_id,
            symbol=symbol,
            asset_tier="core",
            timeframe=timeframe,
            interval="30m",
            indicator=feature_name.split(".")[0],
            event_key=f"event-{feature_name}-{subtype}-{symbol}-{timeframe}-{key_suffix}",
            feature_name=feature_name,
            direction=direction,
            event_ts=event_ts,
            event_price=100.0,
            strength=0.7,
            subtype=subtype,
            source_payload_key=feature_name,
            context={},
        )
        session.add(event)
        await session.flush()
        session.add(
            FeatureLabel(
                feature_event_id=event.id,
                horizon="30m",
                return_pct=return_pct,
                mfe=max(return_pct, 0.0) + 0.002,
                mae=min(return_pct, 0.0) - 0.001,
                future_price=100.0 * (1 + return_pct),
                future_at=event.event_ts + timedelta(minutes=30),
                status="labeled",
            )
        )


@pytest.mark.asyncio
async def test_feature_candidate_screen_promotes_stable_feature_and_rejects_weak(session) -> None:
    strong_returns = [0.012, 0.01, 0.009, 0.008, 0.011, -0.002]
    weak_returns = [0.004, -0.006, -0.005, 0.002, -0.004, -0.003]
    await add_labeled_feature(session, feature_name="inst_choch", subtype="CHoCH_Bullish", returns=strong_returns)
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=strong_returns,
        symbol="ETHUSDT",
    )
    await add_labeled_feature(session, feature_name="trend_purity", subtype="weak", returns=weak_returns)
    await session.commit()

    report = await feature_candidate_screen(
        session,
        horizon="30m",
        min_samples=6,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        segment_min_samples=3,
        min_segments=2,
        persist=True,
    )
    experiments = await session.execute(select(ExperimentRun))

    assert report["candidate_count"] == 1
    assert report["candidates"][0]["feature_key"] == "inst_choch:CHoCH_Bullish:long"
    assert report["candidates"][0]["paper_ab_ready"] is True
    assert report["all_features"][-1]["promotion_status"] == "rejected"
    assert "win_rate_below_minimum" in report["all_features"][-1]["rejection_reasons"]
    assert report["experiment_run"]["status"] == "research"
    assert experiments.scalar_one().name == "feature_candidates_30m"


@pytest.mark.asyncio
async def test_latest_feature_candidate_screen_reads_persisted_report(session) -> None:
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=[0.012, 0.01, 0.009, 0.008, 0.011, -0.002],
    )
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=[0.012, 0.01, 0.009, 0.008, 0.011, -0.002],
        symbol="ETHUSDT",
    )
    await session.commit()

    missing = await latest_feature_candidate_screen(session, horizon="30m")
    report = await feature_candidate_screen(
        session,
        horizon="30m",
        min_samples=6,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        segment_min_samples=3,
        min_segments=2,
        persist=True,
    )
    latest = await latest_feature_candidate_screen(session, horizon="30m")

    assert missing["materialized"] is False
    assert report["candidate_count"] == 1
    assert latest["materialized"] is True
    assert latest["candidate_count"] == 1
    assert latest["source_experiment_run_id"] is not None


@pytest.mark.asyncio
async def test_generate_default_research_reports_persists_all_reports(session) -> None:
    await add_labeled_feature(session, feature_name="inst_choch", subtype="CHoCH_Bullish", returns=[0.012] * 6)
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=[0.012] * 6,
        symbol="ETHUSDT",
    )
    await session.commit()

    result = await generate_default_research_reports(session, horizon="30m", min_samples=6, limit=100)
    experiments = await session.execute(select(ExperimentRun))

    assert result["generated_count"] == 4
    assert result["error_count"] == 0
    assert {item.name for item in experiments.scalars().all()} == {
        "feature_candidates_30m",
        "feature_paper_ab_30m",
        "feature_segment_candidates_30m",
        "feature_segment_paper_ab_30m",
    }


@pytest.mark.asyncio
async def test_feature_candidate_screen_keeps_single_segment_on_watchlist(session) -> None:
    await add_labeled_feature(
        session,
        feature_name="liquidity_sweep",
        subtype="buy_sweep",
        returns=[0.012, 0.01, 0.011, -0.001, 0.009, 0.008],
    )
    await session.commit()

    report = await feature_candidate_screen(
        session,
        horizon="30m",
        min_samples=6,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        segment_min_samples=3,
        min_segments=2,
    )

    assert report["candidate_count"] == 0
    assert report["watchlist_count"] == 1
    assert report["watchlist"][0]["promotion_status"] == "watchlist"
    assert report["watchlist"][0]["used_for_opening_decisions"] is False


@pytest.mark.asyncio
async def test_feature_paper_ab_is_report_only_and_does_not_open_paper_trades(session) -> None:
    strong_returns = [0.012, 0.01, 0.009, 0.008, 0.011, -0.002]
    control_returns = [-0.004, 0.002, -0.003, 0.001, -0.002, 0.0]
    await add_labeled_feature(session, feature_name="inst_choch", subtype="CHoCH_Bullish", returns=strong_returns)
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=strong_returns,
        symbol="ETHUSDT",
    )
    await add_labeled_feature(session, feature_name="trend_purity", subtype="control", returns=control_returns)
    await session.commit()

    report = await feature_paper_ab(
        session,
        horizon="30m",
        min_samples=6,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        segment_min_samples=3,
        min_segments=2,
        persist=True,
    )
    paper_count = await session.scalar(select(func.count()).select_from(PaperTrade))
    experiment = await session.scalar(select(ExperimentRun).where(ExperimentRun.name == "feature_paper_ab_30m"))

    assert report["policy"]["opens_paper_trades"] is False
    assert report["policy"]["changes_strategy_weights"] is False
    assert report["selected_candidate_count"] == 1
    assert report["arms"]["candidate"]["trade_count"] == 12
    assert report["arms"]["control"]["trade_count"] == 6
    assert report["arms"]["edge"]["avg_return_delta"] > 0
    assert paper_count == 0
    assert experiment is not None
    assert experiment.status == "research"


@pytest.mark.asyncio
async def test_segment_candidate_screen_promotes_local_segments_when_global_feature_is_weak(session) -> None:
    strong_returns = [0.012, 0.01, 0.009, 0.008, 0.011, -0.002]
    weak_returns = [-0.006, -0.005, -0.004, -0.003, 0.001, -0.002]
    await add_labeled_feature(session, feature_name="inst_choch", subtype="CHoCH_Bullish", returns=strong_returns)
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=weak_returns,
        symbol="ZECUSDT",
    )
    await session.commit()

    global_report = await feature_candidate_screen(
        session,
        horizon="30m",
        min_samples=6,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        segment_min_samples=3,
        min_segments=2,
    )
    segment_report = await feature_segment_candidate_screen(
        session,
        horizon="30m",
        min_samples=6,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        min_unique_time_buckets=1,
        min_unique_event_days=1,
        min_unique_market_windows=1,
        min_unique_collection_runs=1,
        max_same_return_samples=6,
        max_return_cluster_ratio=1.0,
        persist=True,
    )
    experiment = await session.scalar(select(ExperimentRun).where(ExperimentRun.name == "feature_segment_candidates_30m"))

    assert global_report["candidate_count"] == 0
    assert segment_report["candidate_count"] == 1
    assert segment_report["candidates"][0]["segment_key"] == "inst_choch:CHoCH_Bullish:long:BTCUSDT:short"
    assert segment_report["by_feature"][0]["feature_key"] == "inst_choch:CHoCH_Bullish:long"
    assert segment_report["candidates"][0]["used_for_opening_decisions"] is False
    assert experiment is not None
    assert experiment.status == "research"


@pytest.mark.asyncio
async def test_segment_candidate_screen_marks_clustered_segments_as_high_risk(session) -> None:
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=[0.01, 0.011, 0.012, 0.013, 0.014, 0.015],
        event_spacing_minutes=1,
    )
    await session.commit()

    report = await feature_segment_candidate_screen(
        session,
        horizon="30m",
        min_samples=3,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        min_unique_time_buckets=3,
        max_same_return_samples=10,
    )
    row = report["all_segments"][0]

    assert report["candidate_count"] == 0
    assert row["raw_sample_count"] == 6
    assert row["sample_count"] == 1
    assert row["quality"]["unique_time_bucket_count"] == 1
    assert row["overfit_risk"] == "high"
    assert "time_bucket_count_below_minimum" in row["rejection_reasons"]


@pytest.mark.asyncio
async def test_segment_candidate_screen_requires_cross_day_and_run_diversity(session) -> None:
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=[0.012, 0.01, 0.009, 0.008, 0.011, -0.002],
        event_spacing_minutes=31,
    )
    await session.commit()

    report = await feature_segment_candidate_screen(
        session,
        horizon="30m",
        min_samples=6,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        min_unique_time_buckets=3,
        min_unique_event_days=2,
        min_unique_market_windows=2,
        min_unique_collection_runs=2,
        max_same_return_samples=6,
        max_return_cluster_ratio=1.0,
    )
    row = report["all_segments"][0]

    assert report["candidate_count"] == 0
    assert row["quality"]["unique_event_day_count"] == 1
    assert row["quality"]["unique_market_window_count"] == 1
    assert row["quality"]["unique_collection_run_count"] >= 1
    assert "event_day_count_below_minimum" in row["rejection_reasons"]
    assert "market_window_count_below_minimum" in row["rejection_reasons"]


@pytest.mark.asyncio
async def test_segment_candidate_screen_accepts_cross_day_run_diverse_segments(session) -> None:
    run_ids = ["run-1", "run-2"]
    for index, run_id in enumerate(run_ids):
        session.add(
            CollectionRun(
                id=run_id,
                status="completed",
                dry_run=False,
                requested_assets=["BTC"],
                requested_timeframes=["short"],
                requested_indicators=["inst_choch"],
                snapshots_written=3,
                prices_written=1,
                errors=[],
                started_at=datetime(2026, 1, 1 + index, tzinfo=timezone.utc),
                finished_at=datetime(2026, 1, 1 + index, 1, tzinfo=timezone.utc),
            )
        )
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=[0.012, 0.01, 0.009],
        event_spacing_minutes=31,
        start_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        create_snapshots=True,
        collection_run_ids=["run-1"],
    )
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=[0.008, 0.011, -0.002],
        event_spacing_minutes=31,
        start_ts=datetime(2026, 1, 2, tzinfo=timezone.utc),
        create_snapshots=True,
        collection_run_ids=["run-2"],
    )
    await session.commit()

    report = await feature_segment_candidate_screen(
        session,
        horizon="30m",
        min_samples=6,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        min_unique_time_buckets=3,
        min_unique_event_days=2,
        min_unique_market_windows=2,
        min_unique_collection_runs=2,
        max_same_return_samples=6,
        max_return_cluster_ratio=1.0,
    )
    row = report["candidates"][0]

    assert report["candidate_count"] == 1
    assert row["quality"]["unique_event_day_count"] == 2
    assert row["quality"]["unique_collection_run_count"] == 2
    assert row["quality"]["unique_market_window_count"] == 2
    assert report["quality_summary"]["max_unique_event_day_count"] == 2


@pytest.mark.asyncio
async def test_segment_candidate_screen_balances_limited_samples_across_days(session) -> None:
    for run_id, started_at in [
        ("run-old", datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ("run-new", datetime(2026, 1, 2, tzinfo=timezone.utc)),
    ]:
        session.add(
            CollectionRun(
                id=run_id,
                status="completed",
                dry_run=False,
                requested_assets=["BTC"],
                requested_timeframes=["short"],
                requested_indicators=["inst_choch"],
                snapshots_written=20,
                prices_written=1,
                errors=[],
                started_at=started_at,
                finished_at=started_at + timedelta(hours=2),
            )
        )
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=[0.012, 0.011, 0.01],
        event_spacing_minutes=31,
        start_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        create_snapshots=True,
        collection_run_ids=["run-old"],
    )
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=[0.009, 0.008, 0.007, 0.006, 0.005, 0.004, 0.003, 0.002],
        event_spacing_minutes=31,
        start_ts=datetime(2026, 1, 2, tzinfo=timezone.utc),
        create_snapshots=True,
        collection_run_ids=["run-new"],
    )
    await session.commit()

    report = await feature_segment_candidate_screen(
        session,
        horizon="30m",
        min_samples=6,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        min_unique_time_buckets=3,
        min_unique_event_days=2,
        min_unique_market_windows=2,
        min_unique_collection_runs=2,
        max_same_return_samples=6,
        max_return_cluster_ratio=1.0,
        limit=6,
    )
    row = report["candidates"][0]

    assert report["labeled_count"] == 6
    assert report["candidate_count"] == 1
    assert row["quality"]["unique_event_day_count"] == 2
    assert row["quality"]["unique_collection_run_count"] == 2


@pytest.mark.asyncio
async def test_segment_paper_ab_is_report_only_and_uses_matched_controls(session) -> None:
    strong_returns = [0.012, 0.01, 0.009, 0.008, 0.011, -0.002]
    weak_returns = [-0.006, -0.005, -0.004, -0.003, 0.001, -0.002]
    control_returns = [-0.004, 0.002, -0.003, 0.001, -0.002, 0.0]
    await add_labeled_feature(session, feature_name="inst_choch", subtype="CHoCH_Bullish", returns=strong_returns)
    await add_labeled_feature(session, feature_name="trend_purity", subtype="control", returns=control_returns)
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=weak_returns,
        symbol="ZECUSDT",
    )
    await session.commit()

    report = await feature_segment_paper_ab(
        session,
        horizon="30m",
        min_samples=6,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        min_unique_time_buckets=1,
        min_unique_event_days=1,
        min_unique_market_windows=1,
        min_unique_collection_runs=1,
        max_same_return_samples=6,
        max_return_cluster_ratio=1.0,
        persist=True,
    )
    paper_count = await session.scalar(select(func.count()).select_from(PaperTrade))
    experiment = await session.scalar(select(ExperimentRun).where(ExperimentRun.name == "feature_segment_paper_ab_30m"))

    assert report["policy"]["opens_paper_trades"] is False
    assert report["selected_candidate_count"] == 1
    assert report["arms"]["candidate"]["trade_count"] == 6
    assert report["data_quality"]["raw_candidate_pseudo_trade_count"] == 6
    assert report["arms"]["matched_control"]["trade_count"] == 6
    assert report["arms"]["matched_edge"]["avg_return_delta"] > 0
    assert paper_count == 0
    assert experiment is not None
    assert experiment.status == "research"


@pytest.mark.asyncio
async def test_feature_candidate_reports_conservative_profit_factor_lower(session) -> None:
    await add_labeled_feature(
        session,
        feature_name="liquidity_vacuum",
        subtype="gap_up",
        returns=[0.01] * 30,
        event_spacing_minutes=31,
    )
    await add_labeled_feature(
        session,
        feature_name="liquidity_vacuum",
        subtype="gap_up",
        returns=[0.01] * 30,
        symbol="ETHUSDT",
        event_spacing_minutes=31,
    )
    await session.commit()

    report = await feature_candidate_screen(
        session,
        horizon="30m",
        min_samples=30,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        segment_min_samples=30,
        min_segments=2,
    )
    row = report["candidates"][0]

    assert row["profit_factor"] == 999.0
    assert row["profit_factor_lower"] < row["profit_factor"]
    assert row["win_rate_lower"] < row["win_rate"]
    assert row["reliability_score"] is not None


@pytest.mark.asyncio
async def test_segment_candidate_reports_time_split_validation(session) -> None:
    returns = [0.01] * 27 + [-0.02, -0.02, -0.02]
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="late_decay",
        returns=returns,
        event_spacing_minutes=31,
    )
    await session.commit()

    report = await feature_segment_candidate_screen(
        session,
        horizon="30m",
        min_samples=30,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        min_unique_time_buckets=3,
        min_unique_event_days=1,
        min_unique_market_windows=1,
        min_unique_collection_runs=1,
        max_same_return_samples=30,
        max_return_cluster_ratio=1.0,
    )
    row = report["all_segments"][0]

    assert row["time_split"]["status"] == "decayed"
    assert "decayed" in row["rejection_reasons"]
    assert row["time_split"]["splits"]["recent"]["avg_return"] < 0
