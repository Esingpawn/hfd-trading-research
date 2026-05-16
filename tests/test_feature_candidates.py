from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import CollectionRun, ExperimentRun, FeatureEvent, FeatureLabel, PaperTrade, SignalSnapshot
from app.services.feature_candidates import (
    DEFAULT_RESEARCH_REPORT_STATEMENT_TIMEOUT_MS,
    feature_candidate_screen,
    feature_paper_ab,
    feature_segment_candidate_screen,
    feature_segment_paper_ab,
    generate_default_research_reports,
    latest_feature_candidate_screen,
    research_report_freshness,
    research_report_statement_timeout_ms,
)


async def add_labeled_feature_item(
    session,
    *,
    feature_name: str,
    subtype: str,
    symbol: str,
    timeframe: str,
    event_ts: datetime,
    return_pct: float,
    direction: str = "long",
) -> None:
    key_suffix = f"{event_ts.strftime('%Y%m%d%H%M')}-{symbol}-{timeframe}"
    event = FeatureEvent(
        snapshot_id=f"snapshot-{feature_name}-{subtype}-{key_suffix}",
        symbol=symbol,
        asset_tier="core",
        timeframe=timeframe,
        interval="30m",
        indicator=feature_name.split(".")[0],
        event_key=f"event-{feature_name}-{subtype}-{direction}-{key_suffix}",
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
async def test_latest_feature_candidate_screen_prefers_deeper_report(session) -> None:
    old_deep = ExperimentRun(
        name="feature_candidates_30m",
        status="research",
        metrics={"horizon": "30m", "limit": 100000, "labeled_count": 100000, "candidate_count": 3},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    newer_light = ExperimentRun(
        name="feature_candidates_30m",
        status="research",
        metrics={"horizon": "30m", "limit": 5000, "labeled_count": 5000, "candidate_count": 0},
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    session.add_all([old_deep, newer_light])
    await session.commit()

    latest = await latest_feature_candidate_screen(session, horizon="30m")

    assert latest["limit"] == 100000
    assert latest["labeled_count"] == 100000
    assert latest["candidate_count"] == 3
    assert latest["source_experiment_run_id"] == old_deep.id


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
    assert report["policy"]["lineage"]["lineage"] == "legacy_feature_research"
    assert report["policy"]["lineage"]["legacy_control"] is True
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
    assert segment_report["policy"]["lineage"]["lineage"] == "legacy_feature_research"
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


@pytest.mark.asyncio
async def test_feature_candidate_reliability_score_discounts_tiny_samples(session) -> None:
    await add_labeled_feature(
        session,
        feature_name="micro_poc",
        subtype="single_win",
        returns=[0.05],
    )
    await session.commit()

    report = await feature_segment_candidate_screen(
        session,
        horizon="30m",
        min_samples=30,
        max_same_return_samples=30,
        max_return_cluster_ratio=1.0,
    )
    row = report["all_segments"][0]

    assert row["sample_count"] == 1
    assert row["reliability_score"] < 0.1


@pytest.mark.asyncio
async def test_segment_candidate_sampling_prefers_cross_segment_coverage(session) -> None:
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(60):
        await add_labeled_feature_item(
            session,
            feature_name="micro_poc",
            subtype="crowded",
            symbol="BTCUSDT",
            timeframe="short",
            event_ts=base_ts + timedelta(minutes=index * 31),
            return_pct=0.01,
        )
    for index in range(6):
        await add_labeled_feature_item(
            session,
            feature_name="inst_vwap",
            subtype="thin",
            symbol="ETHUSDT",
            timeframe="short",
            event_ts=base_ts + timedelta(days=index, minutes=index * 31),
            return_pct=0.012,
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
        min_unique_collection_runs=1,
        max_same_return_samples=60,
        max_return_cluster_ratio=1.0,
        limit=12,
    )
    rows = {row["segment_key"]: row for row in report["all_segments"]}

    assert rows["inst_vwap:thin:long:ETHUSDT:short"]["sample_count"] == 6
    assert rows["inst_vwap:thin:long:ETHUSDT:short"]["quality"]["unique_event_day_count"] == 6
    assert rows["micro_poc:crowded:long:BTCUSDT:short"]["sample_count"] == 6


@pytest.mark.asyncio
async def test_segment_candidate_sampling_fills_diverse_segments_to_min_samples_first(session) -> None:
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for segment_index in range(20):
        symbol = f"S{segment_index:02d}USDT"
        for item_index in range(30):
            await add_labeled_feature_item(
                session,
                feature_name=f"feature_{segment_index:02d}",
                subtype="diverse",
                symbol=symbol,
                timeframe="short",
                event_ts=base_ts + timedelta(days=item_index % 3, minutes=item_index * 31),
                return_pct=0.01,
            )
    for segment_index in range(40):
        symbol = f"L{segment_index:02d}USDT"
        for item_index in range(5):
            await add_labeled_feature_item(
                session,
                feature_name=f"long_tail_{segment_index:02d}",
                subtype="thin",
                symbol=symbol,
                timeframe="short",
                event_ts=base_ts + timedelta(minutes=segment_index * 10 + item_index),
                return_pct=0.002,
            )
    await session.commit()

    report = await feature_segment_candidate_screen(
        session,
        horizon="30m",
        min_samples=30,
        min_win_rate=0.6,
        min_profit_factor=1.2,
        min_unique_time_buckets=3,
        min_unique_event_days=2,
        min_unique_market_windows=2,
        min_unique_collection_runs=1,
        max_same_return_samples=60,
        max_return_cluster_ratio=1.0,
        limit=300,
    )
    full_segments = [row for row in report["all_segments"] if row["sample_count"] >= 30]

    assert report["labeled_count"] == 300
    assert len(full_segments) == 10
    assert all(row["feature_name"].startswith("feature_") for row in full_segments)


@pytest.mark.asyncio
async def test_generate_default_research_reports_uses_shared_balanced_sample(session) -> None:
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(40):
        await add_labeled_feature_item(
            session,
            feature_name="micro_poc",
            subtype="crowded",
            symbol="BTCUSDT",
            timeframe="short",
            event_ts=base_ts + timedelta(minutes=index * 31),
            return_pct=0.01,
        )
    for index in range(6):
        await add_labeled_feature_item(
            session,
            feature_name="inst_vwap",
            subtype="thin",
            symbol="ETHUSDT",
            timeframe="short",
            event_ts=base_ts + timedelta(days=index, minutes=index * 31),
            return_pct=0.012,
        )
    await session.commit()

    result = await generate_default_research_reports(session, horizon="30m", min_samples=6, limit=12)
    segment_experiment = await session.scalar(select(ExperimentRun).where(ExperimentRun.name == "feature_segment_candidates_30m"))
    rows = {row["segment_key"]: row for row in segment_experiment.metrics["all_segments"]}

    assert result["labeled_count"] == 12
    assert result["reports"]["feature_candidates"]["candidate_count"] == 0
    assert rows["inst_vwap:thin:long:ETHUSDT:short"]["sample_count"] == 6


@pytest.mark.asyncio
async def test_generate_default_research_reports_caps_requested_limit(session) -> None:
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=[0.012] * 8,
    )
    await session.commit()

    result = await generate_default_research_reports(session, horizon="30m", min_samples=6, limit=999999)

    assert result["requested_limit"] == 999999
    assert result["limit"] == 5000
    assert result["generated_count"] == 4
    assert result["reports"]["feature_candidates"]["requested_limit"] == 999999
    assert result["reports"]["feature_candidates"]["limit"] == 5000
    assert result["reports"]["feature_candidates"]["limit_capped"] is True


@pytest.mark.asyncio
async def test_generate_default_research_reports_honors_env_limit(session, monkeypatch) -> None:
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=[0.012] * 8,
    )
    await session.commit()
    monkeypatch.setenv("HFD_RESEARCH_QUERY_MAX_LIMIT", "12000")

    result = await generate_default_research_reports(session, horizon="30m", min_samples=6, limit=999999)

    assert result["requested_limit"] == 999999
    assert result["limit"] == 12000
    assert result["reports"]["feature_candidates"]["limit"] == 12000
    assert result["reports"]["feature_candidates"]["limit_capped"] is True


@pytest.mark.asyncio
async def test_generate_default_research_reports_skips_when_reports_are_fresh(session) -> None:
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=[0.012] * 8,
    )
    await session.commit()

    first = await generate_default_research_reports(session, horizon="30m", min_samples=6, limit=100)
    second = await generate_default_research_reports(session, horizon="30m", min_samples=6, limit=100)
    freshness = await research_report_freshness(session, horizon="30m")

    assert first["generated_count"] == 4
    assert second["status"] == "skipped"
    assert second["skip_reason"] == "research_reports_fresh"
    assert second["generated_count"] == 0
    assert freshness["fresh"] is True


@pytest.mark.asyncio
async def test_research_report_entrypoints_cap_requested_limit(session) -> None:
    await add_labeled_feature(
        session,
        feature_name="inst_choch",
        subtype="CHoCH_Bullish",
        returns=[0.012] * 8,
    )
    await session.commit()

    candidate_report = await feature_candidate_screen(session, horizon="30m", min_samples=6, limit=999999)
    paper_report = await feature_paper_ab(session, horizon="30m", min_samples=6, limit=999999)
    segment_report = await feature_segment_candidate_screen(session, horizon="30m", min_samples=6, limit=999999)
    segment_paper_report = await feature_segment_paper_ab(session, horizon="30m", min_samples=6, limit=999999)

    for report in [candidate_report, paper_report, segment_report, segment_paper_report]:
        assert report["requested_limit"] == 999999
        assert report["limit"] == 5000
        assert report["limit_capped"] is True


def test_research_statement_timeout_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("HFD_RESEARCH_REPORT_STATEMENT_TIMEOUT_MS", "120000")

    assert research_report_statement_timeout_ms() == 120000


def test_research_statement_timeout_falls_back_for_invalid_env(monkeypatch) -> None:
    monkeypatch.setenv("HFD_RESEARCH_REPORT_STATEMENT_TIMEOUT_MS", "not-an-int")

    assert research_report_statement_timeout_ms() == DEFAULT_RESEARCH_REPORT_STATEMENT_TIMEOUT_MS


def test_research_statement_timeout_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("HFD_RESEARCH_REPORT_STATEMENT_TIMEOUT_MS", "999999")

    assert research_report_statement_timeout_ms() == 300000
