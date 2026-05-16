from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import DarkflowInteraction, PriceSnapshot, ShadowPaperTrade, SignalSnapshot, TradeCandidate
from app.services.darkflow_candidate_promotion import (
    DARKFLOW_V2_SHADOW_STRATEGY_NAME,
    audit_darkflow_trade_candidates,
    darkflow_candidate_promotion_report,
    darkflow_entry_plan_state_report,
    open_darkflow_shadow_forward_samples,
)
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
    assert card["entry_plan"]["plan_type"] == "frozen_darkflow_v2_entry_plan"
    assert card["entry_plan"]["state"] == "frozen"
    assert card["entry_plan"]["planned_entry"] == 100.0
    assert card["entry_plan"]["planned_stop"] == 99.0
    assert card["entry_plan"]["take_profit_levels"][0]["price"] == 102.0
    assert card["entry_plan"]["entry_reference_price"] == 100.0
    assert card["entry_plan"]["entry_range"] == {
        "lower": 99.4,
        "upper": 100.6,
        "source": "entry_reference_tolerance",
    }
    assert card["entry_plan"]["valid_until"] == "2026-01-01T02:00:00+00:00"
    assert "entry_range_missed" in card["entry_plan"]["invalidation_rules"]
    assert card["risk"]["rr_ratio"] == 2.0
    assert card["risk_gate"]["paper_eligible"] is False
    assert "anti_repaint_audit_missing" in card["risk_gate"]["promotion_blockers"]


@pytest.mark.asyncio
async def test_decision_cards_accept_historical_v2_context_without_schema_marker(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        DarkflowInteraction(
            interaction_key="decision-historical-v2",
            zone_key="zone-historical-v2",
            source_snapshot_id="snapshot-historical-v2",
            symbol="BTCUSDT",
            timeframe="short",
            interval="30m",
            indicator="trend_price",
            playbook="trend_ride_extension",
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
                "evidence": {"trend_alignment": {"aligned": True}},
                "target_plan": {"model": "tutorial_dynamic_zone_target_v1"},
                "quality": {
                    "score": 82.0,
                    "confirmations": ["official_rule_mapped", "dynamic_darkflow_target"],
                    "blockers": [],
                },
            },
        )
    )
    await session.commit()

    report = await latest_darkflow_decision_cards(session, limit=5)

    assert report["card_count"] == 1
    card = report["cards"][0]
    assert card["card_id"].endswith("decision-historical-v2")
    assert card["context"]["interaction_schema"] == "v2"


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

    assert refreshed["updated"] == 1
    assert candidate is not None
    assert candidate.anti_repaint_status == "passed"
    assert candidate.shadow_status == "collecting"
    assert candidate.promotion_status == "shadow_running"
    assert "anti_repaint_audit_missing" not in candidate.promotion_blockers
    assert "isolated_v2_shadow_forward_sample_collecting" in candidate.promotion_blockers


@pytest.mark.asyncio
async def test_trade_candidate_audit_passes_rebuildable_candidates(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-audit",
            zone_key="zone-candidate-audit",
            source_snapshot_id="snapshot-candidate-audit",
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
                "evidence": {"trend_alignment": {"aligned": True}},
                "target_plan": {"model": "tutorial_dynamic_zone_target_v1"},
                "quality": {
                    "score": 92.0,
                    "confirmations": ["official_rule_mapped", "dynamic_darkflow_target"],
                    "blockers": [],
                },
            },
        )
    )
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)

    result = await audit_darkflow_trade_candidates(session, limit=10)
    candidate = await session.scalar(select(TradeCandidate))

    assert result["passed"] == 1
    assert candidate is not None
    assert candidate.anti_repaint_status == "passed"
    assert candidate.promotion_status == "shadow_forward_pending"
    assert "anti_repaint_audit_missing" not in candidate.promotion_blockers
    assert "isolated_v2_shadow_forward_sample_missing" in candidate.promotion_blockers


@pytest.mark.asyncio
async def test_shadow_forward_opens_isolated_v2_sample_after_audit(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-shadow-forward",
            zone_key="zone-candidate-shadow-forward",
            source_snapshot_id="snapshot-candidate-shadow-forward",
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
                "evidence": {"trend_alignment": {"aligned": True}},
                "target_plan": {"model": "tutorial_dynamic_zone_target_v1"},
                "quality": {
                    "score": 92.0,
                    "confirmations": ["official_rule_mapped", "dynamic_darkflow_target"],
                    "blockers": [],
                },
            },
        )
    )
    session.add(
        PriceSnapshot(
            symbol="BTCUSDT",
            price=100.0,
            raw_payload={},
            collected_at=base + timedelta(minutes=5),
            created_at=base + timedelta(minutes=5),
        )
    )
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)
    await audit_darkflow_trade_candidates(session, limit=10)

    result = await open_darkflow_shadow_forward_samples(
        session,
        limit=10,
        max_candidate_age_hours=0,
        entry_tolerance_pct=0.05,
    )
    candidate = await session.scalar(select(TradeCandidate))
    trades = (await session.execute(select(ShadowPaperTrade))).scalars().all()
    report = await darkflow_candidate_promotion_report(session, limit=10)

    assert len(result["opened"]) == 1
    assert len(trades) == 1
    assert trades[0].strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME
    assert trades[0].context["opens_paper_trades"] is False
    assert trades[0].context["opens_live_orders"] is False
    assert candidate is not None
    assert candidate.shadow_status == "collecting"
    assert candidate.promotion_status == "shadow_forward_collecting"
    assert candidate.paper_eligible is False
    assert candidate.live_eligible is False
    assert report["shadow_status_counts"] == {"collecting": 1}


@pytest.mark.asyncio
async def test_shadow_forward_waits_when_price_has_not_reached_frozen_entry_range(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-shadow-waiting",
            zone_key="zone-candidate-shadow-waiting",
            source_snapshot_id="snapshot-candidate-shadow-waiting",
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
                "quality": {"score": 92.0, "confirmations": ["official_rule_mapped"], "blockers": []},
            },
        )
    )
    session.add(
        PriceSnapshot(
            symbol="BTCUSDT",
            price=99.2,
            raw_payload={},
            collected_at=base + timedelta(minutes=5),
            created_at=base + timedelta(minutes=5),
        )
    )
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)
    await audit_darkflow_trade_candidates(session, limit=10)

    result = await open_darkflow_shadow_forward_samples(
        session,
        limit=10,
        max_candidate_age_hours=0,
        entry_tolerance_pct=0.05,
    )
    trades = (await session.execute(select(ShadowPaperTrade))).scalars().all()

    assert result["opened"] == []
    assert len(trades) == 0
    assert result["skipped"][0]["reason"] == "entry_plan_waiting"
    assert result["skipped"][0]["entry_plan_state"]["state"] == "waiting"


@pytest.mark.asyncio
async def test_shadow_forward_skips_missed_frozen_entry_range(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-shadow-missed",
            zone_key="zone-candidate-shadow-missed",
            source_snapshot_id="snapshot-candidate-shadow-missed",
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
                "quality": {"score": 92.0, "confirmations": ["official_rule_mapped"], "blockers": []},
            },
        )
    )
    session.add(
        PriceSnapshot(
            symbol="BTCUSDT",
            price=101.0,
            raw_payload={},
            collected_at=base + timedelta(minutes=5),
            created_at=base + timedelta(minutes=5),
        )
    )
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)
    await audit_darkflow_trade_candidates(session, limit=10)

    result = await open_darkflow_shadow_forward_samples(
        session,
        limit=10,
        max_candidate_age_hours=0,
        entry_tolerance_pct=0.05,
    )
    trades = (await session.execute(select(ShadowPaperTrade))).scalars().all()

    assert result["opened"] == []
    assert len(trades) == 0
    assert result["skipped"][0]["reason"] == "entry_plan_missed"
    assert result["skipped"][0]["entry_plan_state"]["reason"] == "entry_range_missed"


@pytest.mark.asyncio
async def test_shadow_forward_skips_invalidated_frozen_entry_range(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-shadow-invalidated",
            zone_key="zone-candidate-shadow-invalidated",
            source_snapshot_id="snapshot-candidate-shadow-invalidated",
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
            exit_price=99.0,
            exit_ts=base + timedelta(minutes=30),
            exit_reason="stop_loss",
            pnl_pct=-0.01,
            r_multiple=-1.0,
            mfe=0.003,
            mae=-0.012,
            status="backtested",
            context={
                "interaction_schema": "v2",
                "quality": {"score": 92.0, "confirmations": ["official_rule_mapped"], "blockers": []},
            },
        )
    )
    session.add(
        PriceSnapshot(
            symbol="BTCUSDT",
            price=98.9,
            raw_payload={},
            collected_at=base + timedelta(minutes=5),
            created_at=base + timedelta(minutes=5),
        )
    )
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)
    await audit_darkflow_trade_candidates(session, limit=10)

    result = await open_darkflow_shadow_forward_samples(
        session,
        limit=10,
        max_candidate_age_hours=0,
        entry_tolerance_pct=0.05,
    )
    trades = (await session.execute(select(ShadowPaperTrade))).scalars().all()

    assert result["opened"] == []
    assert len(trades) == 0
    assert result["skipped"][0]["reason"] == "entry_plan_invalidated"
    assert result["skipped"][0]["entry_plan_state"]["reason"] == "price_crosses_invalidation"


@pytest.mark.asyncio
async def test_entry_plan_state_report_summarizes_frozen_candidate_states(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    session.add(
        SignalSnapshot(
            symbol="BTCUSDT",
            asset_tier="core",
            timeframe="short",
            interval="30m",
            indicator="trend_price",
            endpoint="/api/pro/pro_data",
            raw_payload={},
            summary_payload={},
            collected_at=base,
            created_at=base,
        )
    )
    rows = [
        ("state-waiting", "BTCUSDT", 99.2),
        ("state-missed", "ETHUSDT", 101.0),
        ("state-triggered", "SOLUSDT", 100.0),
        ("state-invalidated", "LINKUSDT", 98.9),
    ]
    for key, symbol, price in rows:
        session.add(
            DarkflowInteraction(
                interaction_key=key,
                zone_key=f"zone-{key}",
                source_snapshot_id=f"snapshot-{key}",
                symbol=symbol,
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
                    "quality": {"score": 92.0, "confirmations": ["official_rule_mapped"], "blockers": []},
                },
            )
        )
        session.add(
            PriceSnapshot(
                symbol=symbol,
                price=price,
                raw_payload={},
                collected_at=base + timedelta(minutes=5),
                created_at=base + timedelta(minutes=5),
            )
        )
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)

    report = await darkflow_entry_plan_state_report(session, limit=10, entry_tolerance_pct=0.05)

    assert report["candidate_count"] == 4
    assert report["state_counts"] == {"waiting": 1, "missed": 1, "triggered": 1, "invalidated": 1}
    assert report["freshness"]["status"] == "fresh"
    assert report["freshness"]["pipeline"]["expected_worker"] == "experiment-worker"
    assert report["policy"]["report_only"] is True
    assert report["policy"]["mutates_candidate_state"] is False
    assert report["policy"]["opens_live_orders"] is False


@pytest.mark.asyncio
async def test_entry_plan_state_report_marks_missing_price_and_expired(session) -> None:
    expired_base = datetime.now(timezone.utc) - timedelta(hours=8)
    fresh_base = datetime.now(timezone.utc) - timedelta(minutes=5)
    for key, symbol, base in (
        ("state-expired", "BTCUSDT", expired_base),
        ("state-missing", "ETHUSDT", fresh_base),
    ):
        session.add(
            DarkflowInteraction(
                interaction_key=key,
                zone_key=f"zone-{key}",
                source_snapshot_id=f"snapshot-{key}",
                symbol=symbol,
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
                    "quality": {"score": 92.0, "confirmations": ["official_rule_mapped"], "blockers": []},
                },
            )
        )
    session.add(
        PriceSnapshot(
            symbol="BTCUSDT",
            price=100.0,
            raw_payload={},
            collected_at=expired_base + timedelta(minutes=5),
            created_at=expired_base + timedelta(minutes=5),
        )
    )
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)

    report = await darkflow_entry_plan_state_report(session, limit=10, entry_tolerance_pct=0.05)

    assert report["state_counts"] == {"missing_price": 1, "expired": 1}
    assert report["reason_counts"]["missing_latest_price"] == 1
    assert report["reason_counts"]["valid_until_passed"] == 1
    assert report["missing_price_count"] == 1
