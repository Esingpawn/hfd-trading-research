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
    reset_feature_research,
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
    assert events[0].event_ts == item.collected_at
    assert events[0].event_price == rows[1][2]
    assert events[0].strength == 0.7
    assert events[1].direction == "short"
    assert events[0].event_key != events[1].event_key


def test_extract_feature_events_uses_indicator_source_whitelist() -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "inst_vwap": [[rows[1][0], rows[1][2]]],
            "order_blocks": [{"start_time": rows[1][0], "avg_price": rows[1][2], "type": "Accumulation"}],
            "micro_poc": [{"start_time": rows[2][0], "poc_price": rows[2][2], "type": "Distribution"}],
        }
    )
    item.indicator = "inst_vwap"

    events = extract_feature_events(item)

    assert events == []


def test_extract_feature_events_uses_experiment_indicator_payloads() -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "fair_value_gap": [
                {"start_time": rows[1][0], "gap_low": 98.0, "gap_high": 99.0, "direction": "bullish", "score": 0.8},
            ],
        }
    )
    item.indicator = "fair_value_gap"

    events = extract_feature_events(item)

    assert len(events) == 1
    assert events[0].indicator == "fair_value_gap"
    assert events[0].feature_name == "fair_value_gap"
    assert events[0].source_payload_key == "fair_value_gap"
    assert events[0].direction == "long"
    assert events[0].event_price == 98.5


def test_extract_feature_events_uses_liquidity_vacuum_alias_payloads() -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "vacuum_zones": [
                {"timestamp": rows[2][0], "lower_price": 104.0, "upper_price": 106.0, "type": "bearish_vacuum", "size": 2.0},
            ],
        }
    )
    item.indicator = "liquidity_vacuum"

    events = extract_feature_events(item)

    assert len(events) == 1
    assert events[0].feature_name == "liquidity_vacuum.vacuum_zones"
    assert events[0].source_payload_key == "vacuum_zones"
    assert events[0].direction == "short"
    assert events[0].event_price == 105.0


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
    item.collected_at = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)
    session.add(item)
    observed_at = item.collected_at
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
    assert report["label_quality"]["labeled_label_ratio"] >= 0
    assert report["features"][0]["sample_count"] == 2
    assert report["features"][0]["avg_return"] > 0
    assert report["by_direction"][0]["name"] == "long"


@pytest.mark.asyncio
async def test_backfill_feature_labels_can_refresh_labeled_rows(session) -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "inst_choch": [{"timestamp": rows[0][0], "price": 100.0, "type": "CHoCH_Bullish"}],
        }
    )
    item.collected_at = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)
    session.add(item)
    for minutes, price in [(0, 100.0), (30, 101.0)]:
        session.add(
            PriceSnapshot(
                symbol="BTCUSDT",
                price=price,
                raw_payload={},
                collected_at=item.collected_at + timedelta(minutes=minutes),
            )
        )
    await session.commit()

    await backfill_feature_events(session, limit=10)
    first = await backfill_feature_labels(session, limit=10, horizons=["30m"])
    second = await backfill_feature_labels(session, limit=10, horizons=["30m"])
    refreshed = await backfill_feature_labels(session, limit=10, horizons=["30m"], refresh_labeled=True)

    assert first.labels_labeled == 1
    assert second.labels_labeled == 0
    assert refreshed.labels_labeled == 1
    assert refreshed.labels_refreshed == 1


@pytest.mark.asyncio
async def test_backfill_feature_labels_advances_past_completed_latest_events(session) -> None:
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    completed = FeatureEvent(
        snapshot_id="snapshot-completed",
        symbol="BTCUSDT",
        asset_tier="core",
        timeframe="short",
        interval="30m",
        indicator="inst_choch",
        event_key="event-completed",
        feature_name="inst_choch",
        direction="long",
        event_ts=base_ts + timedelta(hours=1),
        event_price=100.0,
        strength=0.7,
        subtype="CHoCH_Bullish",
        source_payload_key="inst_choch",
        context={},
    )
    incomplete = FeatureEvent(
        snapshot_id="snapshot-incomplete",
        symbol="BTCUSDT",
        asset_tier="core",
        timeframe="short",
        interval="30m",
        indicator="inst_choch",
        event_key="event-incomplete",
        feature_name="inst_choch",
        direction="long",
        event_ts=base_ts,
        event_price=100.0,
        strength=0.7,
        subtype="CHoCH_Bullish",
        source_payload_key="inst_choch",
        context={},
    )
    session.add_all([completed, incomplete])
    await session.flush()
    session.add(
        FeatureLabel(
            feature_event_id=completed.id,
            horizon="30m",
            return_pct=0.01,
            mfe=0.01,
            mae=0.0,
            future_price=101.0,
            future_at=base_ts + timedelta(hours=1, minutes=30),
            status="labeled",
        )
    )
    session.add_all(
        [
            PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=base_ts),
            PriceSnapshot(symbol="BTCUSDT", price=101.0, raw_payload={}, collected_at=base_ts + timedelta(minutes=30)),
        ]
    )
    await session.commit()

    result = await backfill_feature_labels(session, limit=1, horizons=["30m"])
    labels = await session.execute(select(FeatureLabel).where(FeatureLabel.feature_event_id == incomplete.id))

    assert result.events_scanned == 1
    assert result.labels_labeled == 1
    assert labels.scalar_one().status == "labeled"


@pytest.mark.asyncio
async def test_backfill_feature_labels_skips_unmatured_latest_events(session) -> None:
    now = datetime.now(timezone.utc)
    recent_ts = now - timedelta(minutes=35)
    mature_ts = now - timedelta(hours=2)
    recent = FeatureEvent(
        snapshot_id="snapshot-recent",
        symbol="BTCUSDT",
        asset_tier="core",
        timeframe="short",
        interval="30m",
        indicator="inst_choch",
        event_key="event-recent",
        feature_name="inst_choch",
        direction="long",
        event_ts=recent_ts,
        event_price=100.0,
        strength=0.7,
        subtype="CHoCH_Bullish",
        source_payload_key="inst_choch",
        context={},
    )
    mature = FeatureEvent(
        snapshot_id="snapshot-mature",
        symbol="BTCUSDT",
        asset_tier="core",
        timeframe="short",
        interval="30m",
        indicator="inst_choch",
        event_key="event-mature",
        feature_name="inst_choch",
        direction="long",
        event_ts=mature_ts,
        event_price=100.0,
        strength=0.7,
        subtype="CHoCH_Bullish",
        source_payload_key="inst_choch",
        context={},
    )
    session.add_all([recent, mature])
    session.add_all(
        [
            PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=mature_ts),
            PriceSnapshot(symbol="BTCUSDT", price=101.0, raw_payload={}, collected_at=mature_ts + timedelta(minutes=30)),
            PriceSnapshot(symbol="BTCUSDT", price=102.0, raw_payload={}, collected_at=now - timedelta(minutes=10)),
        ]
    )
    await session.commit()

    result = await backfill_feature_labels(session, limit=1, horizons=["30m"])
    labels = await session.execute(select(FeatureLabel))
    stored = labels.scalar_one()

    assert result.events_scanned == 1
    assert result.labels_labeled == 1
    assert stored.feature_event_id == mature.id


@pytest.mark.asyncio
async def test_backfill_feature_labels_handles_multi_horizons_independently(session) -> None:
    event_ts = datetime.now(timezone.utc) - timedelta(hours=2)
    event = FeatureEvent(
        snapshot_id="snapshot-multi-horizon",
        symbol="BTCUSDT",
        asset_tier="core",
        timeframe="short",
        interval="30m",
        indicator="inst_choch",
        event_key="event-multi-horizon",
        feature_name="inst_choch",
        direction="long",
        event_ts=event_ts,
        event_price=100.0,
        strength=0.7,
        subtype="CHoCH_Bullish",
        source_payload_key="inst_choch",
        context={},
    )
    session.add(event)
    session.add_all(
        [
            PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=event_ts),
            PriceSnapshot(symbol="BTCUSDT", price=101.0, raw_payload={}, collected_at=event_ts + timedelta(minutes=30)),
            PriceSnapshot(symbol="BTCUSDT", price=102.0, raw_payload={}, collected_at=event_ts + timedelta(hours=1)),
        ]
    )
    await session.commit()

    result = await backfill_feature_labels(session, limit=4, horizons=["30m", "1h", "4h", "24h"])
    rows = await session.execute(select(FeatureLabel))
    stored = rows.scalars().all()

    assert result.events_scanned == 2
    assert result.labels_labeled == 2
    assert result.labels_pending == 0
    assert result.horizon_results == {
        "30m": {"events_scanned": 1, "labels_labeled": 1, "labels_pending": 0, "labels_skipped": 0, "labels_refreshed": 0},
        "1h": {"events_scanned": 1, "labels_labeled": 1, "labels_pending": 0, "labels_skipped": 0, "labels_refreshed": 0},
        "4h": {"events_scanned": 0, "labels_labeled": 0, "labels_pending": 0, "labels_skipped": 0, "labels_refreshed": 0},
        "24h": {"events_scanned": 0, "labels_labeled": 0, "labels_pending": 0, "labels_skipped": 0, "labels_refreshed": 0},
    }
    assert sorted(label.horizon for label in stored) == ["1h", "30m"]


@pytest.mark.asyncio
async def test_backfill_feature_labels_prioritizes_existing_pending_rows(session) -> None:
    base_ts = datetime.now(timezone.utc) - timedelta(hours=6)
    pending_event = FeatureEvent(
        snapshot_id="snapshot-pending-first",
        symbol="BTCUSDT",
        asset_tier="core",
        timeframe="short",
        interval="30m",
        indicator="inst_choch",
        event_key="event-pending-first",
        feature_name="inst_choch",
        direction="long",
        event_ts=base_ts,
        event_price=100.0,
        strength=0.7,
        subtype="CHoCH_Bullish",
        source_payload_key="inst_choch",
        context={},
    )
    newer_event = FeatureEvent(
        snapshot_id="snapshot-newer-unlabeled",
        symbol="BTCUSDT",
        asset_tier="core",
        timeframe="short",
        interval="30m",
        indicator="inst_choch",
        event_key="event-newer-unlabeled",
        feature_name="inst_choch",
        direction="long",
        event_ts=base_ts + timedelta(hours=1),
        event_price=100.0,
        strength=0.7,
        subtype="CHoCH_Bullish",
        source_payload_key="inst_choch",
        context={},
    )
    session.add_all([pending_event, newer_event])
    await session.flush()
    session.add(
        FeatureLabel(
            feature_event_id=pending_event.id,
            horizon="1h",
            return_pct=None,
            mfe=None,
            mae=None,
            future_price=None,
            future_at=None,
            status="pending",
        )
    )
    session.add_all(
        [
            PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=base_ts),
            PriceSnapshot(symbol="BTCUSDT", price=101.0, raw_payload={}, collected_at=base_ts + timedelta(hours=1)),
            PriceSnapshot(symbol="BTCUSDT", price=102.0, raw_payload={}, collected_at=base_ts + timedelta(hours=2)),
        ]
    )
    await session.commit()

    result = await backfill_feature_labels(session, limit=1, horizons=["1h"])
    labels = await session.execute(select(FeatureLabel).where(FeatureLabel.feature_event_id == pending_event.id))

    assert result.events_scanned == 1
    assert result.labels_labeled == 1
    assert result.labels_refreshed == 1
    assert labels.scalar_one().status == "labeled"


@pytest.mark.asyncio
async def test_reset_feature_research_deletes_events_and_labels(session) -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "inst_choch": [{"timestamp": rows[0][0], "price": 100.0, "type": "CHoCH_Bullish"}],
        }
    )
    item.collected_at = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)
    session.add(item)
    for minutes, price in [(0, 100.0), (30, 101.0)]:
        session.add(
            PriceSnapshot(
                symbol="BTCUSDT",
                price=price,
                raw_payload={},
                collected_at=item.collected_at + timedelta(minutes=minutes),
            )
        )
    await session.commit()

    await backfill_feature_events(session, limit=10)
    await backfill_feature_labels(session, limit=10, horizons=["30m"])
    result = await reset_feature_research(session)
    stored_events = await session.execute(select(FeatureEvent))
    stored_labels = await session.execute(select(FeatureLabel))

    assert result.events_deleted == 1
    assert result.labels_deleted == 1
    assert stored_events.scalars().all() == []
    assert stored_labels.scalars().all() == []


@pytest.mark.asyncio
async def test_backfill_feature_labels_keeps_stale_future_prices_pending(session) -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "inst_choch": [{"timestamp": rows[0][0], "price": 100.0, "type": "CHoCH_Bullish"}],
        }
    )
    item.collected_at = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)
    session.add(item)
    observed_at = item.collected_at
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
    assert labels.labels_pending == 1
    assert stored.status == "pending"
    assert stored.return_pct is None


@pytest.mark.asyncio
async def test_backfill_feature_events_infers_hvn_support_resistance(session) -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "hvn_nodes": [{"price": 90.0, "volume": 5000}],
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

    events = await backfill_feature_events(session, limit=10)
    labels = await backfill_feature_labels(session, limit=10, horizons=["30m"])
    stored_events = await session.execute(select(FeatureEvent))
    stored = stored_events.scalar_one()

    assert events.events_extracted == 1
    assert events.events_inserted == 1
    assert labels.events_scanned == 1
    assert stored.indicator == "hvn_nodes"
    assert stored.direction == "long"
    assert stored.feature_name == "hvn_nodes"


@pytest.mark.asyncio
async def test_backfill_feature_labels_skips_mismatched_event_price(session) -> None:
    rows = klines()
    item = snapshot(
        {
            "klines": rows,
            "inst_choch": [{"timestamp": rows[0][0], "price": 1.0, "type": "CHoCH_Bullish"}],
        }
    )
    item.collected_at = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)
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
