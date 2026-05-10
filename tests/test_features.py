from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import FeatureEvent, FeatureLabel, PriceSnapshot, SignalSnapshot
from app.services.features import (
    backfill_feature_events,
    backfill_feature_labels,
    extract_feature_events,
    feature_effectiveness,
    summarize_signal_payload,
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


def klines(count: int = 8) -> list[list[float]]:
    start = 1_700_000_000_000
    rows = []
    for index in range(count):
        price = 100 + index
        rows.append([start + index * 1_800_000, price, price + 0.5, price - 0.5, price + 1.0, 10])
    return rows


def snapshot(payload: dict) -> SignalSnapshot:
    return SignalSnapshot(
        symbol="BTCUSDT",
        asset_tier="core",
        timeframe="short",
        interval="30m",
        indicator="inst_choch",
        endpoint="/api/pro/pro_data",
        raw_payload=payload,
        summary_payload={},
        collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_summarize_signal_payload_extracts_kline_bounds() -> None:
    payload = {
        "klines": [
            [1000, 10, 11, 9, 12, 100],
            [2000, 11, 12, 10, 13, 120],
        ],
        "smart_money_cost": [{"avg_price": 11.5}],
    }

    summary = summarize_signal_payload(payload, "smart_money_cost")

    assert summary["kline_count"] == 2
    assert summary["first_kline_ts"] == 1000
    assert summary["last_kline_ts"] == 2000
    assert summary["last_close"] == 12
    assert summary["indicator_item_count"] == 1


def test_extract_feature_events_normalizes_indicator_items() -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "inst_choch": [
                {"timestamp": rows[1][0], "price": rows[1][2], "type": "CHoCH_Bullish", "confidence": 0.7},
                {"timestamp": rows[2][0], "price": rows[2][2], "type": "BOS_Bearish", "confidence": 0.6},
            ],
        }
    )

    events = extract_feature_events(item)

    assert len(events) == 2
    assert events[0].feature_name == "inst_choch"
    assert events[0].direction == "long"
    assert events[0].event_price == rows[1][2]
    assert events[0].strength == 0.7
    assert events[1].direction == "short"
    assert events[0].event_key != events[1].event_key


@pytest.mark.asyncio
async def test_backfill_feature_events_is_idempotent(session) -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "inst_choch": [
                {"timestamp": rows[1][0], "price": rows[1][2], "type": "CHoCH_Bullish"},
                {"timestamp": rows[2][0], "price": rows[2][2], "type": "BOS_Bullish"},
            ],
        }
    )
    session.add(item)
    await session.commit()

    first = await backfill_feature_events(session, limit=10)
    second = await backfill_feature_events(session, limit=10)
    stored = await session.execute(select(FeatureEvent))

    assert first.events_inserted == 2
    assert second.events_inserted == 0
    assert second.duplicates == 2
    assert len(stored.scalars().all()) == 2


@pytest.mark.asyncio
async def test_backfill_feature_events_deduplicates_recollected_same_event(session) -> None:
    rows = klines()
    payload = {
        "klines": rows,
        "inst_choch": [{"timestamp": rows[1][0], "price": rows[1][2], "type": "CHoCH_Bullish"}],
    }
    session.add(snapshot(payload))
    session.add(snapshot(payload))
    await session.commit()

    result = await backfill_feature_events(session, limit=10)
    stored = await session.execute(select(FeatureEvent))

    assert result.events_extracted == 2
    assert result.events_inserted == 1
    assert result.duplicates == 1
    assert len(stored.scalars().all()) == 1


@pytest.mark.asyncio
async def test_backfill_feature_labels_and_effectiveness(session) -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "inst_choch": [
                {"timestamp": rows[0][0], "price": 100.0, "type": "CHoCH_Bullish"},
                {"timestamp": rows[1][0], "price": 101.0, "type": "CHoCH_Bullish"},
            ],
        }
    )
    session.add(item)
    observed_at = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)
    for minutes, price in [(0, 100.0), (30, 101.0), (60, 102.0), (240, 108.0), (1440, 110.0)]:
        session.add(
            PriceSnapshot(
                symbol="BTCUSDT",
                price=price,
                raw_payload={},
                collected_at=observed_at + timedelta(minutes=minutes),
            )
        )
    await session.commit()

    events = await backfill_feature_events(session, limit=10)
    labels = await backfill_feature_labels(session, limit=10, horizons=["30m"])
    report = await feature_effectiveness(session, horizon="30m", min_samples=1)
    stored_labels = await session.execute(select(FeatureLabel))

    assert events.events_inserted == 2
    assert labels.labels_labeled == 2
    assert len(stored_labels.scalars().all()) == 2
    assert report["policy"]["used_for_opening_decisions"] is False
    assert report["features"][0]["sample_count"] == 2
    assert report["features"][0]["avg_return"] > 0


@pytest.mark.asyncio
async def test_backfill_feature_labels_keeps_stale_future_prices_pending(session) -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "inst_choch": [{"timestamp": rows[0][0], "price": 100.0, "type": "CHoCH_Bullish"}],
        }
    )
    session.add(item)
    observed_at = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)
    session.add(
        PriceSnapshot(
            symbol="BTCUSDT",
            price=150.0,
            raw_payload={},
            collected_at=observed_at + timedelta(days=3),
        )
    )
    await session.commit()

    await backfill_feature_events(session, limit=10)
    labels = await backfill_feature_labels(session, limit=10, horizons=["30m"])
    stored_labels = await session.execute(select(FeatureLabel))

    stored = stored_labels.scalar_one()
    assert labels.labels_skipped == 1
    assert stored.status == "skipped"
    assert stored.return_pct is None


@pytest.mark.asyncio
async def test_backfill_feature_labels_skips_neutral_events(session) -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "hvn_nodes": [{"price": 100.0, "volume": 5000}],
        }
    )
    item.indicator = "hvn_nodes"
    session.add(item)
    observed_at = item.collected_at
    for minutes, price in [(0, 100.0), (30, 101.0)]:
        session.add(
            PriceSnapshot(
                symbol="BTCUSDT",
                price=price,
                raw_payload={},
                collected_at=observed_at + timedelta(minutes=minutes),
            )
        )
    await session.commit()

    await backfill_feature_events(session, limit=10)
    labels = await backfill_feature_labels(session, limit=10, horizons=["30m"])
    stored_labels = await session.execute(select(FeatureLabel))

    stored = stored_labels.scalar_one()
    assert labels.labels_skipped == 1
    assert stored.status == "skipped"
    assert stored.return_pct is None


@pytest.mark.asyncio
async def test_backfill_feature_labels_skips_mismatched_event_price(session) -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "inst_choch": [{"timestamp": rows[0][0], "price": 1.0, "type": "CHoCH_Bullish"}],
        }
    )
    session.add(item)
    observed_at = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)
    for minutes, price in [(0, 100.0), (30, 101.0)]:
        session.add(
            PriceSnapshot(
                symbol="BTCUSDT",
                price=price,
                raw_payload={},
                collected_at=observed_at + timedelta(minutes=minutes),
            )
        )
    await session.commit()

    await backfill_feature_events(session, limit=10)
    labels = await backfill_feature_labels(session, limit=10, horizons=["30m"])
    stored_labels = await session.execute(select(FeatureLabel))

    stored = stored_labels.scalar_one()
    assert labels.labels_skipped == 1
    assert stored.status == "skipped"
    assert stored.return_pct is None
