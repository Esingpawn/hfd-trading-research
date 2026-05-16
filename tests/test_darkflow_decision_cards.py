from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import DarkflowInteraction, TradeCandidate
from app.services.darkflow_decision_cards import (
    latest_darkflow_decision_cards,
    latest_materialized_trade_candidates,
    materialize_darkflow_trade_candidates,
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


@pytest.mark.asyncio
async def test_decision_cards_build_from_core_darkflow_v2_only(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        DarkflowInteraction(
            interaction_key="decision-core",
            zone_key="zone-core",
            source_snapshot_id="snapshot-core",
            symbol="BTCUSDT",
            timeframe="short",
            interval="30m",
            indicator="trend_price",
            playbook="pullback_to_cost",
            direction="long",
            interaction_type="wick_pierce_reclaim",
            event_ts=base,
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
            invalidation_price=99.0,
            exit_price=102.0,
            exit_ts=base + timedelta(minutes=30),
            exit_reason="target_hit",
            pnl_pct=0.02,
            r_multiple=2.0,
            mfe=0.025,
            mae=-0.004,
            status="backtested",
            context={
                "interaction_schema": "v2",
                "hold_bars": 4,
                "target_plan": {"model": "tutorial_dynamic_zone_target_v1"},
                "quality": {
                    "score": 82.0,
                    "confirmations": ["official_rule_mapped", "dynamic_darkflow_target"],
                    "blockers": [],
                },
            },
        )
    )
    session.add(
        DarkflowInteraction(
            interaction_key="decision-legacy",
            zone_key="zone-legacy",
            source_snapshot_id="snapshot-legacy",
            symbol="BTCUSDT",
            timeframe="short",
            interval="30m",
            indicator="trend_price",
            playbook="pullback_to_cost",
            direction="long",
            interaction_type="wick_pierce_reclaim",
            event_ts=base + timedelta(minutes=1),
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
            invalidation_price=99.0,
            exit_price=102.0,
            exit_ts=base + timedelta(minutes=30),
            exit_reason="target_hit",
            pnl_pct=0.02,
            r_multiple=2.0,
            mfe=0.025,
            mae=-0.004,
            status="backtested",
            context={"quality": {"score": 90.0, "confirmations": [], "blockers": []}},
        )
    )
    await session.commit()

    report = await latest_darkflow_decision_cards(session, limit=5)

    assert report["policy"]["opens_live_orders"] is False
    assert report["policy"]["opens_paper_trades"] is False
    assert report["policy"]["lineage"]["lineage"] == "core_darkflow_v2"
    assert report["card_count"] == 1
    card = report["cards"][0]
    assert card["card_id"].endswith("decision-core")
    assert card["strategy_id"] == "pullback_to_cost"
    assert card["entry_plan"]["planned_entry"] == 100.0
    assert card["entry_plan"]["planned_stop"] == 99.0
    assert card["entry_plan"]["take_profit_levels"][0]["price"] == 102.0
    assert card["risk"]["rr_ratio"] == 2.0
    assert card["risk_gate"]["paper_eligible"] is False
    assert "anti_repaint_audit_missing" in card["risk_gate"]["promotion_blockers"]


@pytest.mark.asyncio
async def test_decision_cards_block_weak_or_conflicting_quality(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        DarkflowInteraction(
            interaction_key="decision-blocked",
            zone_key="zone-blocked",
            source_snapshot_id="snapshot-blocked",
            symbol="ETHUSDT",
            timeframe="short",
            interval="30m",
            indicator="liquidity_sweep",
            playbook="liquidity_sweep_reversal",
            direction="long",
            interaction_type="first_touch",
            event_ts=base,
            entry_price=100.0,
            stop_price=99.0,
            target_price=100.8,
            invalidation_price=99.0,
            exit_price=99.0,
            exit_ts=base + timedelta(minutes=30),
            exit_reason="stop_loss",
            pnl_pct=-0.01,
            r_multiple=-1.0,
            mfe=0.004,
            mae=-0.012,
            status="backtested",
            context={
                "interaction_schema": "v2",
                "quality": {
                    "score": 45.0,
                    "confirmations": ["official_rule_mapped"],
                    "blockers": ["parent_trend_conflict"],
                },
            },
        )
    )
    await session.commit()

    report = await latest_darkflow_decision_cards(session, limit=5)
    card = report["cards"][0]

    assert card["risk_gate"]["status"] == "research_blocked"
    assert card["risk_gate"]["blockers"] == [
        "quality_score_below_threshold",
        "rr_ratio_below_threshold",
        "parent_trend_conflict",
    ]


@pytest.mark.asyncio
async def test_trade_candidates_materialize_core_decision_cards_idempotently(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-core",
            zone_key="zone-candidate-core",
            source_snapshot_id="snapshot-candidate-core",
            symbol="BTCUSDT",
            timeframe="short",
            interval="30m",
            indicator="trend_price",
            playbook="pullback_to_cost",
            direction="long",
            interaction_type="wick_pierce_reclaim",
            event_ts=base,
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
            invalidation_price=99.0,
            exit_price=102.0,
            exit_ts=base + timedelta(minutes=30),
            exit_reason="target_hit",
            pnl_pct=0.02,
            r_multiple=2.0,
            mfe=0.025,
            mae=-0.004,
            status="backtested",
            context={
                "interaction_schema": "v2",
                "quality": {
                    "score": 82.0,
                    "confirmations": ["official_rule_mapped", "dynamic_darkflow_target"],
                    "blockers": [],
                },
            },
        )
    )
    await session.commit()

    first = await materialize_darkflow_trade_candidates(session, limit=10)
    second = await materialize_darkflow_trade_candidates(session, limit=10)
    report = await latest_materialized_trade_candidates(session, limit=10)
    candidates = (await session.execute(select(TradeCandidate))).scalars().all()

    assert first["inserted"] == 1
    assert first["policy"]["opens_paper_trades"] is False
    assert second["inserted"] == 0
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.lineage == "core_darkflow_v2"
    assert candidate.status == "shadow_candidate"
    assert candidate.paper_eligible is False
    assert candidate.live_eligible is False
    assert "anti_repaint_audit_missing" in candidate.promotion_blockers
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["candidate_key"].endswith("candidate-core")


@pytest.mark.asyncio
async def test_trade_candidate_refresh_preserves_lifecycle_when_plan_unchanged(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-preserve",
            zone_key="zone-candidate-preserve",
            source_snapshot_id="snapshot-candidate-preserve",
            symbol="ETHUSDT",
            timeframe="short",
            interval="30m",
            indicator="liquidity_sweep",
            playbook="liquidity_sweep_reversal",
            direction="long",
            interaction_type="first_touch",
            event_ts=base,
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
            invalidation_price=99.0,
            exit_price=102.0,
            exit_ts=base + timedelta(minutes=30),
            exit_reason="target_hit",
            pnl_pct=0.02,
            r_multiple=2.0,
            mfe=0.025,
            mae=-0.004,
            status="backtested",
            context={
                "interaction_schema": "v2",
                "quality": {"score": 80.0, "confirmations": ["official_rule_mapped"], "blockers": []},
            },
        )
    )
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)
    candidate = await session.scalar(select(TradeCandidate))
    assert candidate is not None
    candidate.anti_repaint_status = "passed"
    candidate.shadow_status = "collecting"
    candidate.promotion_status = "shadow_running"
    await session.commit()

    refreshed = await materialize_darkflow_trade_candidates(session, limit=10)
    candidate = await session.scalar(select(TradeCandidate))

    assert refreshed["updated"] == 0
    assert candidate is not None
    assert candidate.anti_repaint_status == "passed"
    assert candidate.shadow_status == "collecting"
    assert candidate.promotion_status == "shadow_running"
