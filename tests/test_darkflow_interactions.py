from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import DarkflowInteraction, DarkflowZone, ExperimentRun, PaperTrade, ShadowPaperTrade, SignalSnapshot
from app.services.darkflow_interactions import (
    DARKFLOW_INTERACTION_STRATEGY,
    backfill_darkflow_interactions,
    darkflow_interaction_backtest,
    darkflow_shadow_replay,
    detect_darkflow_interactions,
    extract_darkflow_zones,
    latest_darkflow_interaction_backtest,
    normalize_klines,
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


def snapshot(payload: dict, *, indicator: str = "trend_price") -> SignalSnapshot:
    return SignalSnapshot(
        symbol="BTCUSDT",
        asset_tier="core",
        timeframe="short",
        interval="30m",
        indicator=indicator,
        endpoint="/api/pro/pro_data",
        raw_payload=payload,
        summary_payload={},
        collected_at=datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc),
    )


def klines() -> list[list[float]]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    prices = [
        (101.0, 102.0, 100.5, 101.5),
        (101.5, 101.8, 99.6, 100.6),
        (100.6, 103.8, 100.4, 103.2),
        (103.2, 104.4, 102.9, 104.0),
    ]
    for index, (open_price, high, low, close) in enumerate(prices):
        ts = int((base + timedelta(minutes=30 * index)).timestamp() * 1000)
        rows.append([ts, open_price, close, low, high, 10.0])
    return rows


def test_extract_darkflow_zones_maps_official_cost_zone() -> None:
    item = snapshot(
        {
            "klines": klines(),
            "trend_price": [
                {"timestamp": klines()[0][0], "lower_price": 99.8, "upper_price": 100.8, "type": "support", "score": 0.9}
            ],
        }
    )

    zones = extract_darkflow_zones(item)

    assert len(zones) == 1
    assert zones[0].family == "cost_structure"
    assert zones[0].zone_type == "cost_zone"
    assert zones[0].direction == "long"
    assert zones[0].lower_price == 99.8
    assert zones[0].upper_price == 100.8
    assert zones[0].context["official_rule"]["family"] == "cost_structure"


def test_detect_darkflow_interactions_marks_reclaim_and_target_hit() -> None:
    item = snapshot(
        {
            "klines": klines(),
            "liquidity_sweep": [
                {"timestamp": klines()[0][0], "lower_price": 100.0, "upper_price": 100.8, "type": "bottom_sweep", "score": 1.0}
            ],
        },
        indicator="liquidity_sweep",
    )
    zone = extract_darkflow_zones(item)[0]
    candles = normalize_klines(item.raw_payload["klines"])

    interactions = detect_darkflow_interactions(zone, candles, max_hold_bars=4)

    assert len(interactions) == 1
    interaction = interactions[0]
    assert interaction.interaction_type == "wick_pierce_reclaim"
    assert interaction.playbook == "liquidity_sweep_reversal"
    assert interaction.exit_reason == "target_hit"
    assert interaction.pnl_pct is not None and interaction.pnl_pct > 0
    assert interaction.r_multiple is not None and interaction.r_multiple > 0


def test_detect_darkflow_interactions_uses_dynamic_darkflow_target_and_quality() -> None:
    item = snapshot(
        {
            "klines": klines(),
            "liquidity_sweep": [
                {"timestamp": klines()[0][0], "lower_price": 100.0, "upper_price": 100.8, "type": "bottom_sweep", "score": 1.0},
                {"timestamp": klines()[0][0], "lower_price": 103.0, "upper_price": 103.4, "type": "top_liquidity", "score": 0.9},
            ],
        },
        indicator="liquidity_sweep",
    )
    zones = extract_darkflow_zones(item)
    candles = normalize_klines(item.raw_payload["klines"])

    interactions = detect_darkflow_interactions(zones[0], candles, max_hold_bars=4, target_zones=zones)

    assert len(interactions) == 1
    context = interactions[0].context
    assert context["target_model"] == "tutorial_dynamic_zone_target_v1"
    assert context["target_plan"]["source"] == "liquidity_sweep.liquidity_zone"
    assert context["quality"]["score"] >= 70
    assert "dynamic_darkflow_target" in context["quality"]["confirmations"]


def test_parent_trend_conflict_blocks_quality_candidate() -> None:
    item = snapshot(
        {
            "klines": klines(),
            "liquidity_sweep": [
                {"timestamp": klines()[0][0], "lower_price": 100.0, "upper_price": 100.8, "type": "bottom_sweep", "score": 1.0},
                {"timestamp": klines()[0][0], "lower_price": 103.0, "upper_price": 103.4, "type": "top_liquidity", "score": 0.9},
            ],
        },
        indicator="liquidity_sweep",
    )
    zones = extract_darkflow_zones(item)
    candles = normalize_klines(item.raw_payload["klines"])

    interactions = detect_darkflow_interactions(
        zones[0],
        candles,
        max_hold_bars=4,
        target_zones=zones,
        trend_context={"states": [{"timeframe": "long", "bias": "short"}]},
    )

    quality = interactions[0].context["quality"]
    assert "parent_trend_conflict" in quality["blockers"]
    assert interactions[0].context["evidence"]["trend_alignment"]["aligned"] is False


@pytest.mark.asyncio
async def test_backfill_interactions_persists_zones_and_is_idempotent(session) -> None:
    item = snapshot(
        {
            "klines": klines(),
            "trend_price": [
                {"timestamp": klines()[0][0], "lower_price": 100.0, "upper_price": 100.8, "type": "support", "score": 0.9}
            ],
        }
    )
    session.add(item)
    await session.commit()

    first = await backfill_darkflow_interactions(session, limit=10)
    second = await backfill_darkflow_interactions(session, limit=10)
    zones = await session.scalar(select(func.count()).select_from(DarkflowZone))
    interactions = await session.scalar(select(func.count()).select_from(DarkflowInteraction))

    assert first.zones_extracted == 1
    assert first.interactions_inserted == 1
    assert second.interactions_inserted == 0
    assert zones == 1
    assert interactions == 1


@pytest.mark.asyncio
async def test_interaction_backtest_persists_latest_and_shadow_replay_is_isolated(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(4):
        session.add(
            DarkflowInteraction(
                interaction_key=f"interaction-{index}",
                zone_key=f"zone-{index}",
                source_snapshot_id=f"snapshot-{index}",
                symbol="BTCUSDT",
                timeframe="short",
                interval="30m",
                indicator="trend_price",
                playbook="pullback_to_cost",
                direction="long",
                interaction_type="first_touch",
                event_ts=base + timedelta(hours=index),
                entry_price=100.0,
                stop_price=99.0,
                target_price=101.8,
                invalidation_price=99.0,
                exit_price=101.8 if index < 3 else 99.0,
                exit_ts=base + timedelta(hours=index, minutes=30),
                exit_reason="target_hit" if index < 3 else "stop_loss",
                pnl_pct=0.018 if index < 3 else -0.01,
                r_multiple=1.8 if index < 3 else -1.0,
                mfe=0.02,
                mae=-0.004,
                status="backtested",
                context={"research_only": True},
            )
        )
    await session.commit()

    report = await darkflow_interaction_backtest(session, min_samples=4, limit=100, persist=True)
    latest = await latest_darkflow_interaction_backtest(session)
    replay = await darkflow_shadow_replay(session, limit=10)
    paper_count = await session.scalar(select(func.count()).select_from(PaperTrade))
    shadow_rows = (await session.execute(select(ShadowPaperTrade))).scalars().all()
    experiment = await session.scalar(select(ExperimentRun).where(ExperimentRun.name == "darkflow_interaction_backtest"))

    assert report["candidate_playbook_count"] == 1
    assert latest["materialized"] is True
    assert experiment is not None and experiment.status == "research"
    assert replay["policy"]["opens_paper_trades"] is False
    assert replay["inserted"] == 4
    assert paper_count == 0
    assert all(row.strategy_name == DARKFLOW_INTERACTION_STRATEGY for row in shadow_rows)
    assert all(row.context["historical_replay"] for row in shadow_rows)


@pytest.mark.asyncio
async def test_interaction_backtest_candidates_use_quality_filtered_samples(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    good_context = {"quality": {"score": 78.0, "confirmations": ["dynamic_darkflow_target"], "blockers": []}}
    weak_context = {"quality": {"score": 30.0, "confirmations": [], "blockers": ["fixed_r_target_fallback"]}}
    conflict_context = {"quality": {"score": 90.0, "confirmations": [], "blockers": ["parent_trend_conflict"]}}
    for index in range(4):
        session.add(
            DarkflowInteraction(
                interaction_key=f"quality-good-{index}",
                zone_key=f"quality-zone-good-{index}",
                source_snapshot_id=f"snapshot-good-{index}",
                symbol="BTCUSDT",
                timeframe="short",
                interval="30m",
                indicator="liquidity_sweep",
                playbook="liquidity_sweep_reversal",
                direction="long",
                interaction_type="wick_pierce_reclaim",
                event_ts=base + timedelta(hours=index),
                entry_price=100.0,
                stop_price=99.0,
                target_price=102.0,
                invalidation_price=99.0,
                exit_price=102.0 if index < 3 else 99.0,
                exit_ts=base + timedelta(hours=index, minutes=30),
                exit_reason="target_hit" if index < 3 else "stop_loss",
                pnl_pct=0.02 if index < 3 else -0.01,
                r_multiple=2.0 if index < 3 else -1.0,
                mfe=0.025,
                mae=-0.004,
                status="backtested",
                context=good_context,
            )
        )
    for index in range(12):
        session.add(
            DarkflowInteraction(
                interaction_key=f"quality-weak-{index}",
                zone_key=f"quality-zone-weak-{index}",
                source_snapshot_id=f"snapshot-weak-{index}",
                symbol="BTCUSDT",
                timeframe="short",
                interval="30m",
                indicator="trend_price",
                playbook="pullback_to_cost",
                direction="long",
                interaction_type="first_touch",
                event_ts=base + timedelta(hours=10 + index),
                entry_price=100.0,
                stop_price=99.0,
                target_price=102.0,
                invalidation_price=99.0,
                exit_price=102.0,
                exit_ts=base + timedelta(hours=10 + index, minutes=30),
                exit_reason="target_hit",
                pnl_pct=0.02,
                r_multiple=2.0,
                mfe=0.025,
                mae=-0.004,
                status="backtested",
                context=weak_context,
            )
        )
    session.add(
        DarkflowInteraction(
            interaction_key="quality-conflict",
            zone_key="quality-zone-conflict",
            source_snapshot_id="snapshot-conflict",
            symbol="BTCUSDT",
            timeframe="short",
            interval="30m",
            indicator="liquidity_sweep",
            playbook="liquidity_sweep_reversal",
            direction="long",
            interaction_type="wick_pierce_reclaim",
            event_ts=base + timedelta(hours=30),
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
            invalidation_price=99.0,
            exit_price=102.0,
            exit_ts=base + timedelta(hours=30, minutes=30),
            exit_reason="target_hit",
            pnl_pct=0.02,
            r_multiple=2.0,
            mfe=0.025,
            mae=-0.004,
            status="backtested",
            context=conflict_context,
        )
    )
    await session.commit()

    report = await darkflow_interaction_backtest(session, min_samples=4, limit=100, min_quality_score=55.0)
    rows = {row["playbook"]: row for row in report["playbooks"]}

    assert report["quality_interaction_count"] == 4
    assert rows["liquidity_sweep_reversal"]["readiness"]["status"] == "candidate"
    assert rows["pullback_to_cost"]["quality_sample_count"] == 0
    assert rows["pullback_to_cost"]["readiness"]["status"] == "rejected"
