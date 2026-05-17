from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import DarkflowInteraction, ExperimentRun, FeatureEvent, FeatureLabel, PriceSnapshot, ShadowPaperTrade, SignalSnapshot, TradeCandidate
from app.services.experiment_loop import run_darkflow_pipeline, run_experiment_backfill


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
async def test_experiment_backfill_maintains_feature_research_labels(session) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        SignalSnapshot(
            symbol="BTCUSDT",
            asset_tier="core",
            timeframe="short",
            interval="30m",
            indicator="inst_choch",
            endpoint="/api/pro/pro_data",
            raw_payload={
                "klines": [[1_700_000_000_000, 100, 100, 99, 101, 10]],
                "inst_choch": [
                    {"timestamp": 1_700_000_000_000, "price": 100.0, "type": "CHoCH_Bullish"},
                ],
            },
            summary_payload={},
            collected_at=observed_at,
        )
    )
    session.add_all(
        [
            PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=observed_at),
            PriceSnapshot(
                symbol="BTCUSDT",
                price=101.0,
                raw_payload={},
                collected_at=observed_at + timedelta(minutes=30),
            ),
        ]
    )
    await session.commit()

    result = await run_experiment_backfill(
        session,
        signal_limit=10,
        feature_limit=10,
        feature_label_limit=10,
        feature_horizons=["30m"],
    )
    events = await session.execute(select(FeatureEvent))
    labels = await session.execute(select(FeatureLabel))

    assert result["features"]["enabled"] is True
    assert result["features"]["events"]["events_inserted"] == 1
    assert result["features"]["labels"]["labels_labeled"] == 1
    assert len(events.scalars().all()) == 1
    assert labels.scalar_one().status == "labeled"


@pytest.mark.asyncio
async def test_experiment_backfill_can_disable_feature_research(session) -> None:
    result = await run_experiment_backfill(
        session,
        signal_limit=10,
        include_feature_research=False,
    )

    assert result["features"] == {"enabled": False}


@pytest.mark.asyncio
async def test_experiment_backfill_maintains_default_feature_horizons_independently(session) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        SignalSnapshot(
            symbol="BTCUSDT",
            asset_tier="core",
            timeframe="short",
            interval="30m",
            indicator="inst_choch",
            endpoint="/api/pro/pro_data",
            raw_payload={
                "klines": [[1_700_000_000_000, 100, 100, 99, 101, 10]],
                "inst_choch": [
                    {"timestamp": 1_700_000_000_000, "price": 100.0, "type": "CHoCH_Bullish"},
                ],
            },
            summary_payload={},
            collected_at=observed_at,
        )
    )
    session.add_all(
        [
            PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=observed_at),
            PriceSnapshot(symbol="BTCUSDT", price=101.0, raw_payload={}, collected_at=observed_at + timedelta(minutes=30)),
        ]
    )
    await session.commit()

    result = await run_experiment_backfill(
        session,
        signal_limit=10,
        feature_limit=10,
        feature_label_limit=8,
    )
    labels = await session.execute(select(FeatureLabel))

    assert result["features"]["horizons"] == ["30m", "1h", "4h", "24h"]
    assert result["features"]["commit_strategy"] == "stage_commits_by_signal_events_and_horizon"
    assert result["features"]["labels"]["labels_labeled"] == 1
    assert result["features"]["labels"]["labels_pending"] == 0
    assert result["features"]["labels"]["horizon_results"]["30m"]["labels_labeled"] == 1
    assert labels.scalar_one().horizon == "30m"


@pytest.mark.asyncio
async def test_experiment_backfill_can_materialize_research_reports(session) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, symbol in enumerate(["BTCUSDT", "ETHUSDT"]):
        session.add(
            SignalSnapshot(
                symbol=symbol,
                asset_tier="core",
                timeframe="short",
                interval="30m",
                indicator="inst_choch",
                endpoint="/api/pro/pro_data",
                raw_payload={
                    "klines": [[1_700_000_000_000, 100, 100, 99, 101, 10]],
                    "inst_choch": [
                        {"timestamp": 1_700_000_000_000, "price": 100.0, "type": "CHoCH_Bullish"},
                    ],
                },
                summary_payload={},
                collected_at=observed_at + timedelta(minutes=index),
            )
        )
    session.add_all(
        [
            PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=observed_at),
            PriceSnapshot(symbol="BTCUSDT", price=101.0, raw_payload={}, collected_at=observed_at + timedelta(minutes=30)),
            PriceSnapshot(symbol="ETHUSDT", price=100.0, raw_payload={}, collected_at=observed_at),
            PriceSnapshot(symbol="ETHUSDT", price=101.0, raw_payload={}, collected_at=observed_at + timedelta(minutes=30)),
        ]
    )
    await session.commit()

    result = await run_experiment_backfill(
        session,
        signal_limit=10,
        feature_limit=10,
        feature_label_limit=10,
        feature_horizons=["30m"],
        include_research_reports=True,
        research_report_min_samples=1,
        research_report_limit=100,
    )
    experiments = await session.execute(select(ExperimentRun))

    assert result["research_reports"]["generated_count"] == 4
    assert result["research_reports"]["error_count"] == 0
    assert len(experiments.scalars().all()) == 4


@pytest.mark.asyncio
async def test_experiment_backfill_skips_fresh_research_reports(session) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, symbol in enumerate(["BTCUSDT", "ETHUSDT"]):
        session.add(
            SignalSnapshot(
                symbol=symbol,
                asset_tier="core",
                timeframe="short",
                interval="30m",
                indicator="inst_choch",
                endpoint="/api/pro/pro_data",
                raw_payload={
                    "klines": [[1_700_000_000_000, 100, 100, 99, 101, 10]],
                    "inst_choch": [
                        {"timestamp": 1_700_000_000_000, "price": 100.0, "type": "CHoCH_Bullish"},
                    ],
                },
                summary_payload={},
                collected_at=observed_at + timedelta(minutes=index),
            )
        )
    session.add_all(
        [
            PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=observed_at),
            PriceSnapshot(symbol="BTCUSDT", price=101.0, raw_payload={}, collected_at=observed_at + timedelta(minutes=30)),
            PriceSnapshot(symbol="ETHUSDT", price=100.0, raw_payload={}, collected_at=observed_at),
            PriceSnapshot(symbol="ETHUSDT", price=101.0, raw_payload={}, collected_at=observed_at + timedelta(minutes=30)),
        ]
    )
    await session.commit()

    first = await run_experiment_backfill(
        session,
        signal_limit=10,
        feature_limit=10,
        feature_label_limit=10,
        feature_horizons=["30m"],
        include_research_reports=True,
        research_report_min_samples=1,
        research_report_limit=100,
    )
    second = await run_experiment_backfill(
        session,
        signal_limit=10,
        feature_limit=10,
        feature_label_limit=10,
        feature_horizons=["30m"],
        include_research_reports=True,
        research_report_min_samples=1,
        research_report_limit=100,
        research_report_max_age_seconds=3600,
    )

    assert first["research_reports"]["generated_count"] == 4
    assert second["research_reports"]["status"] == "skipped"
    assert second["research_reports"]["skip_reason"] == "research_reports_fresh"


@pytest.mark.asyncio
async def test_experiment_backfill_can_maintain_shadow_paper(session) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, symbol in enumerate(["BTCUSDT", "ETHUSDT"]):
        session.add(
            SignalSnapshot(
                symbol=symbol,
                asset_tier="core",
                timeframe="short",
                interval="30m",
                indicator="inst_choch",
                endpoint="/api/pro/pro_data",
                raw_payload={
                    "klines": [[1_700_000_000_000, 100, 100, 99, 101, 10]],
                    "inst_choch": [
                        {"timestamp": 1_700_000_000_000, "price": 100.0, "type": "CHoCH_Bullish"},
                    ],
                },
                summary_payload={},
                collected_at=observed_at + timedelta(minutes=index),
            )
        )
    session.add_all(
        [
            PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=observed_at),
            PriceSnapshot(symbol="BTCUSDT", price=101.0, raw_payload={}, collected_at=observed_at + timedelta(minutes=30)),
            PriceSnapshot(symbol="ETHUSDT", price=100.0, raw_payload={}, collected_at=observed_at),
            PriceSnapshot(symbol="ETHUSDT", price=101.0, raw_payload={}, collected_at=observed_at + timedelta(minutes=30)),
        ]
    )
    await session.commit()

    result = await run_experiment_backfill(
        session,
        signal_limit=10,
        feature_limit=10,
        feature_label_limit=10,
        feature_horizons=["30m"],
        include_research_reports=True,
        research_report_min_samples=1,
        research_report_limit=100,
        include_shadow_paper=True,
        shadow_candidate_limit=5,
    )
    trades = await session.execute(select(ShadowPaperTrade))

    assert result["shadow_paper"]["enabled"] is True
    assert result["shadow_paper"]["scan"]["policy"]["opens_paper_trades"] is False
    assert len(result["shadow_paper"]["scan"]["opened"]) >= 1
    assert len(trades.scalars().all()) >= 1


@pytest.mark.asyncio
async def test_experiment_backfill_can_maintain_darkflow_pipeline(session) -> None:
    await _add_darkflow_snapshot(session)

    result = await run_experiment_backfill(
        session,
        signal_limit=10,
        include_feature_research=False,
        include_darkflow_pipeline=True,
        darkflow_limit=10,
        darkflow_backtest_limit=100,
        darkflow_candidate_limit=10,
        darkflow_shadow_limit=10,
    )
    interactions = await session.execute(select(DarkflowInteraction))
    candidates = await session.execute(select(TradeCandidate))
    shadows = await session.execute(select(ShadowPaperTrade))
    experiments = await session.execute(select(ExperimentRun).where(ExperimentRun.name == "darkflow_interaction_backtest"))
    pipeline_runs = await session.execute(select(ExperimentRun).where(ExperimentRun.name == "darkflow_pipeline_run"))

    assert result["darkflow"]["enabled"] is True
    assert result["darkflow"]["policy"]["opens_live_orders"] is False
    assert result["darkflow"]["policy"]["opens_paper_trades"] is False
    assert result["darkflow"]["interactions"]["interactions_inserted"] == 1
    assert result["darkflow"]["trade_candidates"]["inserted"] == 1
    assert result["darkflow"]["promotion"]["policy"]["opens_live_orders"] is False
    assert len(interactions.scalars().all()) == 1
    assert len(candidates.scalars().all()) == 1
    assert len(shadows.scalars().all()) == 1
    assert experiments.scalar_one().status == "research"
    pipeline_run = pipeline_runs.scalar_one()
    assert pipeline_run.status == "research"
    assert pipeline_run.metrics["stage"] == "completed"


@pytest.mark.asyncio
async def test_darkflow_pipeline_can_run_without_feature_research(session) -> None:
    await _add_darkflow_snapshot(session)

    result = await run_darkflow_pipeline(
        session,
        limit=10,
        backtest_limit=100,
        candidate_limit=10,
        shadow_limit=10,
        max_hold_bars=12,
    )
    interactions = await session.execute(select(DarkflowInteraction))
    candidates = await session.execute(select(TradeCandidate))
    shadows = await session.execute(select(ShadowPaperTrade))

    assert result["enabled"] is True
    assert result["policy"]["opens_live_orders"] is False
    assert result["policy"]["opens_paper_trades"] is False
    assert result["pipeline_run"]["name"] == "darkflow_pipeline_run"
    assert result["pipeline_run"]["status"] == "research"
    assert result["interactions"]["interactions_inserted"] == 1
    assert result["trade_candidates"]["inserted"] == 1
    assert len(interactions.scalars().all()) == 1
    assert len(candidates.scalars().all()) == 1
    assert len(shadows.scalars().all()) == 1


@pytest.mark.asyncio
async def test_darkflow_pipeline_can_skip_zone_persistence_for_lightweight_loop(session) -> None:
    await _add_darkflow_snapshot(session)

    result = await run_darkflow_pipeline(
        session,
        limit=10,
        backtest_limit=100,
        candidate_limit=10,
        shadow_limit=10,
        max_hold_bars=12,
        persist_zones=False,
    )
    interactions = await session.execute(select(DarkflowInteraction))

    assert result["policy"]["persists_darkflow_zones"] is False
    assert result["interactions"]["interactions_inserted"] == 1
    assert len(interactions.scalars().all()) == 1


@pytest.mark.asyncio
async def test_darkflow_pipeline_can_defer_heavy_backtest_for_lightweight_refresh(session) -> None:
    await _add_darkflow_snapshot(session)

    result = await run_darkflow_pipeline(
        session,
        limit=10,
        backtest_limit=100,
        candidate_limit=10,
        shadow_limit=10,
        max_hold_bars=12,
        include_backtest=False,
    )
    pipeline_run = await session.scalar(select(ExperimentRun).where(ExperimentRun.name == "darkflow_pipeline_run"))

    assert result["policy"]["runs_interaction_backtest"] is False
    assert result["interaction_backtest"]["skipped"] is True
    assert result["trade_candidates"]["inserted"] == 1
    assert pipeline_run.metrics["interaction_backtest"]["skipped"] is True


@pytest.mark.asyncio
async def test_darkflow_pipeline_marks_failed_run_when_iteration_errors(session, monkeypatch) -> None:
    async def fail_mark_shadow_paper_trades(_session):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.experiment_loop.mark_shadow_paper_trades", fail_mark_shadow_paper_trades)

    with pytest.raises(RuntimeError, match="boom"):
        await run_darkflow_pipeline(
            session,
            limit=10,
            backtest_limit=100,
            candidate_limit=10,
            shadow_limit=10,
            max_hold_bars=12,
        )

    pipeline_run = await session.scalar(select(ExperimentRun).where(ExperimentRun.name == "darkflow_pipeline_run"))

    assert pipeline_run is not None
    assert pipeline_run.status == "failed"
    assert pipeline_run.metrics["stage"] == "failed"
    assert pipeline_run.metrics["error"] == "boom"


async def _add_darkflow_snapshot(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    rows = []
    for index, (open_price, high, low, close) in enumerate(
        [
            (101.0, 102.0, 100.5, 101.5),
            (101.5, 101.8, 99.6, 100.6),
            (100.6, 103.8, 100.4, 103.2),
            (103.2, 104.4, 102.9, 104.0),
        ]
    ):
        rows.append([int((base + timedelta(minutes=30 * index)).timestamp() * 1000), open_price, close, low, high, 10.0])
    session.add(
        SignalSnapshot(
            symbol="BTCUSDT",
            asset_tier="core",
            timeframe="short",
            interval="30m",
            indicator="liquidity_sweep",
            endpoint="/api/pro/pro_data",
            raw_payload={
                "klines": rows,
                "liquidity_sweep": [
                    {
                        "timestamp": rows[0][0],
                        "lower_price": 100.0,
                        "upper_price": 100.8,
                        "type": "bottom_sweep",
                        "score": 1.0,
                    }
                ],
            },
            summary_payload={},
            collected_at=base + timedelta(minutes=30),
        )
    )
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=base + timedelta(minutes=30)))
    await session.commit()
