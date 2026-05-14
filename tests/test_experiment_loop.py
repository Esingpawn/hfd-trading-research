from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import ExperimentRun, FeatureEvent, FeatureLabel, PriceSnapshot, ShadowPaperTrade, SignalSnapshot
from app.services.experiment_loop import run_experiment_backfill


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
