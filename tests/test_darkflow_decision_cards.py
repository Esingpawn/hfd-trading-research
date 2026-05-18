from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import DarkflowInteraction, ExperimentRun, PriceSnapshot, ShadowPaperTrade, SignalSnapshot, TradeCandidate
from app.services.darkflow_candidate_promotion import (
    DARKFLOW_V2_SHADOW_STRATEGY_NAME,
    audit_darkflow_trade_candidates,
    darkflow_candidate_promotion_report,
    darkflow_entry_plan_state_report,
    open_darkflow_shadow_forward_samples,
    refresh_darkflow_candidate_promotion,
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
async def test_decision_cards_allow_high_quality_sweep_reversal_trend_conflict_into_shadow_research(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        DarkflowInteraction(
            interaction_key="decision-sweep-trend-conflict-shadow-research",
            zone_key="zone-sweep-trend-conflict-shadow-research",
            source_snapshot_id="snapshot-sweep-trend-conflict-shadow-research",
            symbol="ZECUSDT",
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
                "quality": {
                    "score": 55.0,
                    "confirmations": ["official_rule_mapped", "dynamic_darkflow_target"],
                    "blockers": ["parent_trend_conflict"],
                },
            },
        )
    )
    await session.commit()

    report = await latest_darkflow_decision_cards(session, limit=5)
    card = report["cards"][0]

    assert card["risk_gate"]["status"] == "shadow_candidate"
    assert card["risk_gate"]["blockers"] == ["parent_trend_conflict"]
    assert card["risk_gate"]["paper_eligible"] is False
    assert card["risk_gate"]["live_eligible"] is False


@pytest.mark.asyncio
async def test_decision_cards_keep_pullback_trend_conflict_blocked(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        DarkflowInteraction(
            interaction_key="decision-pullback-trend-conflict-blocked",
            zone_key="zone-pullback-trend-conflict-blocked",
            source_snapshot_id="snapshot-pullback-trend-conflict-blocked",
            symbol="BNBUSDT",
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
                    "score": 92.0,
                    "confirmations": ["official_rule_mapped", "dynamic_darkflow_target"],
                    "blockers": ["parent_trend_conflict"],
                },
            },
        )
    )
    await session.commit()

    report = await latest_darkflow_decision_cards(session, limit=5)
    card = report["cards"][0]

    assert card["risk_gate"]["status"] == "research_blocked"
    assert card["risk_gate"]["blockers"] == ["parent_trend_conflict"]


@pytest.mark.asyncio
async def test_decision_cards_allow_low_rr_candidates_into_shadow_research(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        DarkflowInteraction(
            interaction_key="decision-low-rr-shadow-research",
            zone_key="zone-low-rr-shadow-research",
            source_snapshot_id="snapshot-low-rr-shadow-research",
            symbol="HYPEUSDT",
            timeframe="short",
            interval="30m",
            indicator="liquidity_sweep",
            playbook="liquidity_sweep_reversal",
            direction="long",
            interaction_type="first_touch",
            event_ts=base,
            entry_price=100.0,
            stop_price=99.0,
            target_price=101.4,
            invalidation_price=99.0,
            exit_price=101.4,
            exit_ts=base + timedelta(minutes=30),
            exit_reason="target_hit",
            pnl_pct=0.014,
            r_multiple=1.4,
            mfe=0.02,
            mae=-0.004,
            status="backtested",
            context={
                "interaction_schema": "v2",
                "quality": {
                    "score": 92.0,
                    "confirmations": ["official_rule_mapped", "dynamic_darkflow_target"],
                    "blockers": [],
                },
            },
        )
    )
    await session.commit()

    report = await latest_darkflow_decision_cards(session, limit=5)
    card = report["cards"][0]

    assert card["risk"]["rr_ratio"] == 1.4
    assert card["risk_gate"]["status"] == "shadow_candidate"
    assert card["risk_gate"]["blockers"] == ["rr_ratio_below_threshold"]
    assert card["risk_gate"]["paper_eligible"] is False
    assert card["risk_gate"]["live_eligible"] is False


@pytest.mark.asyncio
async def test_decision_cards_allow_marginal_quality_candidates_into_shadow_research(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        DarkflowInteraction(
            interaction_key="decision-marginal-quality-shadow-research",
            zone_key="zone-marginal-quality-shadow-research",
            source_snapshot_id="snapshot-marginal-quality-shadow-research",
            symbol="SOLUSDT",
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
                    "score": 52.0,
                    "confirmations": ["official_rule_mapped"],
                    "blockers": [],
                },
            },
        )
    )
    await session.commit()

    report = await latest_darkflow_decision_cards(session, limit=5)
    card = report["cards"][0]

    assert card["scores"]["quality_score"] == 52.0
    assert card["risk_gate"]["status"] == "shadow_candidate"
    assert card["risk_gate"]["blockers"] == ["quality_score_below_threshold"]
    assert card["risk_gate"]["paper_eligible"] is False
    assert card["risk_gate"]["live_eligible"] is False


@pytest.mark.asyncio
async def test_refresh_opens_low_rr_candidates_for_shadow_but_gate_blocks_review(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-low-rr-shadow-forward",
            zone_key="zone-low-rr-shadow-forward",
            source_snapshot_id="snapshot-low-rr-shadow-forward",
            symbol="BTCUSDT",
            timeframe="short",
            interval="30m",
            indicator="liquidity_sweep",
            playbook="liquidity_sweep_reversal",
            direction="long",
            interaction_type="first_touch",
            event_ts=base,
            entry_price=100.0,
            stop_price=99.0,
            target_price=101.4,
            invalidation_price=99.0,
            exit_price=101.4,
            exit_ts=base + timedelta(minutes=30),
            exit_reason="target_hit",
            pnl_pct=0.014,
            r_multiple=1.4,
            mfe=0.02,
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
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=base, created_at=base))
    await session.commit()

    result = await refresh_darkflow_candidate_promotion(
        session,
        limit=10,
        shadow_limit=10,
        max_candidate_age_hours=0,
        entry_tolerance_pct=0.05,
    )
    candidate = await session.scalar(select(TradeCandidate))
    trades = (await session.execute(select(ShadowPaperTrade))).scalars().all()

    assert len(result["shadow_forward"]["opened"]) == 1
    assert result["shadow_forward"]["opened_count"] == 1
    assert result["shadow_forward"]["skipped_count"] == 0
    assert candidate is not None
    assert candidate.status == "shadow_candidate"
    assert candidate.blockers == ["rr_ratio_below_threshold"]
    assert candidate.shadow_status == "collecting"
    assert candidate.paper_eligible is False
    assert candidate.live_eligible is False
    assert len(trades) == 1
    assert trades[0].context["opens_paper_trades"] is False
    assert trades[0].context["opens_live_orders"] is False

    candidate.shadow_status = "passed"
    candidate.promotion_blockers = []
    await session.commit()

    report = await darkflow_candidate_promotion_report(session, limit=10)
    gate = report["gate_samples"][0]

    assert gate["gate_status"] == "blocked"
    assert gate["primary_blocker"] == "rr_ratio_below_threshold"


@pytest.mark.asyncio
async def test_refresh_opens_marginal_quality_candidates_for_shadow_but_gate_blocks_review(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-marginal-quality-shadow-forward",
            zone_key="zone-marginal-quality-shadow-forward",
            source_snapshot_id="snapshot-marginal-quality-shadow-forward",
            symbol="SOLUSDT",
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
                "quality": {"score": 52.0, "confirmations": ["official_rule_mapped"], "blockers": []},
            },
        )
    )
    session.add(PriceSnapshot(symbol="SOLUSDT", price=100.0, raw_payload={}, collected_at=base, created_at=base))
    await session.commit()

    result = await refresh_darkflow_candidate_promotion(
        session,
        limit=10,
        shadow_limit=10,
        max_candidate_age_hours=0,
        entry_tolerance_pct=0.05,
    )
    candidate = await session.scalar(select(TradeCandidate))
    trades = (await session.execute(select(ShadowPaperTrade))).scalars().all()

    assert len(result["shadow_forward"]["opened"]) == 1
    assert result["shadow_forward"]["opened_count"] == 1
    assert candidate is not None
    assert candidate.status == "shadow_candidate"
    assert candidate.blockers == ["quality_score_below_threshold"]
    assert candidate.shadow_status == "collecting"
    assert len(trades) == 1

    candidate.shadow_status = "passed"
    candidate.promotion_blockers = []
    await session.commit()

    report = await darkflow_candidate_promotion_report(session, limit=10)
    gate = report["gate_samples"][0]

    assert gate["gate_status"] == "blocked"
    assert gate["primary_blocker"] == "quality_score_below_threshold"


@pytest.mark.asyncio
async def test_refresh_opens_sweep_trend_conflict_for_shadow_but_gate_blocks_review(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-sweep-trend-conflict-shadow-forward",
            zone_key="zone-sweep-trend-conflict-shadow-forward",
            source_snapshot_id="snapshot-sweep-trend-conflict-shadow-forward",
            symbol="ZECUSDT",
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
                "quality": {
                    "score": 55.0,
                    "confirmations": ["official_rule_mapped", "dynamic_darkflow_target"],
                    "blockers": ["parent_trend_conflict"],
                },
            },
        )
    )
    session.add(PriceSnapshot(symbol="ZECUSDT", price=100.0, raw_payload={}, collected_at=base, created_at=base))
    await session.commit()

    result = await refresh_darkflow_candidate_promotion(
        session,
        limit=10,
        shadow_limit=10,
        max_candidate_age_hours=0,
        entry_tolerance_pct=0.05,
    )
    candidate = await session.scalar(select(TradeCandidate))
    trades = (await session.execute(select(ShadowPaperTrade))).scalars().all()

    assert len(result["shadow_forward"]["opened"]) == 1
    assert result["shadow_forward"]["opened_count"] == 1
    assert candidate is not None
    assert candidate.status == "shadow_candidate"
    assert candidate.blockers == ["parent_trend_conflict"]
    assert candidate.shadow_status == "collecting"
    assert len(trades) == 1

    candidate.shadow_status = "passed"
    candidate.promotion_blockers = []
    await session.commit()

    report = await darkflow_candidate_promotion_report(session, limit=10)
    gate = report["gate_samples"][0]

    assert gate["gate_status"] == "blocked"
    assert gate["primary_blocker"] == "parent_trend_conflict"


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
async def test_trade_candidates_do_not_materialize_exit_filter_playbooks_as_opening_candidates(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-exit-filter-only",
            zone_key="zone-exit-filter-only",
            source_snapshot_id="snapshot-exit-filter-only",
            symbol="BTCUSDT",
            timeframe="short",
            interval="30m",
            indicator="trend_exhaustion",
            playbook="exhaustion_exit_filter",
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
                "quality": {
                    "score": 95.0,
                    "confirmations": ["official_rule_mapped", "dynamic_darkflow_target"],
                    "blockers": [],
                },
            },
        )
    )
    await session.commit()

    cards = await latest_darkflow_decision_cards(session, limit=10)
    result = await materialize_darkflow_trade_candidates(session, limit=10)
    candidates = (await session.execute(select(TradeCandidate))).scalars().all()

    assert cards["card_count"] == 1
    assert cards["cards"][0]["risk_gate"]["status"] == "research_blocked"
    assert "exit_filter_not_opening_playbook" in cards["cards"][0]["risk_gate"]["blockers"]
    assert result["card_count"] == 1
    assert result["skipped_non_opening_count"] == 1
    assert result["inserted"] == 0
    assert candidates == []


@pytest.mark.asyncio
async def test_trade_candidate_materialize_prefers_opening_playbooks_when_exit_filters_are_newer(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(40):
        session.add(
            DarkflowInteraction(
                interaction_key=f"candidate-newer-exit-filter-{index}",
                zone_key=f"zone-newer-exit-filter-{index}",
                source_snapshot_id=f"snapshot-newer-exit-filter-{index}",
                symbol="BTCUSDT",
                timeframe="short",
                interval="30m",
                indicator="trend_exhaustion",
                playbook="exhaustion_exit_filter",
                direction="long",
                interaction_type="first_touch",
                event_ts=base + timedelta(minutes=index + 1),
                entry_price=100.0,
                stop_price=99.0,
                target_price=102.0,
                invalidation_price=99.0,
                exit_price=102.0,
                exit_ts=base + timedelta(minutes=30 + index),
                exit_reason="target_hit",
                pnl_pct=0.02,
                r_multiple=2.0,
                mfe=0.025,
                mae=-0.004,
                status="backtested",
                context={
                    "interaction_schema": "v2",
                    "quality": {"score": 95.0, "confirmations": ["official_rule_mapped"], "blockers": []},
                },
            )
        )
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-older-opening-playbook",
            zone_key="zone-older-opening-playbook",
            source_snapshot_id="snapshot-older-opening-playbook",
            symbol="ETHUSDT",
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
    await session.commit()

    result = await materialize_darkflow_trade_candidates(session, limit=1)
    candidates = (await session.execute(select(TradeCandidate))).scalars().all()

    assert result["requested_limit"] == 1
    assert result["opening_card_count"] == 1
    assert result["opening_card_fetch_limit"] > result["requested_limit"]
    assert result["skipped_non_opening_count"] > 0
    assert result["inserted"] == 1
    assert len(candidates) == 1
    assert candidates[0].candidate_key.endswith("candidate-older-opening-playbook")
    assert candidates[0].status == "shadow_candidate"


@pytest.mark.asyncio
async def test_trade_candidates_retire_existing_exit_filter_opening_candidates(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-existing-exit-filter",
            zone_key="zone-existing-exit-filter",
            source_snapshot_id="snapshot-existing-exit-filter",
            symbol="BTCUSDT",
            timeframe="short",
            interval="30m",
            indicator="trend_exhaustion",
            playbook="exhaustion_exit_filter",
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
                "quality": {
                    "score": 95.0,
                    "confirmations": ["official_rule_mapped", "dynamic_darkflow_target"],
                    "blockers": [],
                },
            },
        )
    )
    session.add(
        TradeCandidate(
            candidate_key="darkflow-card:v2:candidate-existing-exit-filter",
            source_type="darkflow_interaction",
            source_interaction_id="legacy-source",
            lineage="core_darkflow_v2",
            strategy_family="darkflow_trade_candidates_v1",
            strategy_id="exhaustion_exit_filter",
            strategy_name="耗尽/死亡线退出过滤",
            symbol="BTCUSDT",
            timeframe="short",
            interval="30m",
            direction="long",
            setup_type="first_touch",
            market_state="darkflow_zone_reaction",
            setup_time=base,
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
            rr_ratio=2.0,
            quality_score=95.0,
            rule_score=9.5,
            status="shadow_candidate",
            promotion_status="shadow_forward_pending",
            anti_repaint_status="passed",
            shadow_status="not_started",
            paper_eligible=False,
            live_eligible=False,
            blockers=[],
            promotion_blockers=[],
            supporting_signals=["official_rule_mapped"],
            decision_payload={},
            materialized_at=base,
            updated_at=base,
        )
    )
    await session.commit()

    result = await materialize_darkflow_trade_candidates(session, limit=10)
    candidate = await session.scalar(select(TradeCandidate))

    assert result["inserted"] == 0
    assert result["updated"] == 1
    assert result["skipped_non_opening_count"] == 1
    assert result["rows"][0]["action"] == "retired_non_opening_playbook"
    assert candidate is not None
    assert candidate.status == "entry_plan_retired"
    assert candidate.promotion_status == "entry_plan_retired"
    assert candidate.shadow_status == "retired"
    assert "exit_filter_not_opening_playbook" in candidate.blockers
    assert candidate.decision_payload["non_opening_playbook_retirement"]["reason"] == "playbook_policy_not_opening"


@pytest.mark.asyncio
async def test_trade_candidates_retire_duplicate_exposure_plans_during_materialize(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    targets = [102.0, 103.0]
    for index, target in enumerate(targets):
        session.add(
            DarkflowInteraction(
                interaction_key=f"candidate-materialize-duplicate-{index}",
                zone_key=f"zone-candidate-materialize-duplicate-{index}",
                source_snapshot_id=f"snapshot-candidate-materialize-duplicate-{index}",
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
                target_price=target,
                invalidation_price=99.0,
                exit_price=target,
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

    result = await materialize_darkflow_trade_candidates(session, limit=10)
    candidates = (await session.execute(select(TradeCandidate).order_by(TradeCandidate.target_price))).scalars().all()
    representative = next(candidate for candidate in candidates if candidate.status == "shadow_candidate")
    duplicate = next(candidate for candidate in candidates if candidate.status == "entry_plan_retired")

    assert result["inserted"] == 2
    assert result["duplicate_exposure_count"] == 1
    assert representative.target_price == 103.0
    assert representative.promotion_status == "anti_repaint_pending"
    assert duplicate.shadow_status == "retired"
    assert duplicate.anti_repaint_status == "passed"
    assert duplicate.promotion_status == "duplicate_shadow_plan"
    assert duplicate.promotion_blockers == ["duplicate_shadow_forward_plan"]
    assert duplicate.decision_payload["duplicate_shadow_plan"]["duplicate_of"] == representative.candidate_key

    refreshed = await materialize_darkflow_trade_candidates(session, limit=10)
    await session.refresh(duplicate)

    assert refreshed["inserted"] == 0
    assert duplicate.status == "entry_plan_retired"
    assert duplicate.promotion_status == "duplicate_shadow_plan"


@pytest.mark.asyncio
async def test_trade_candidates_choose_sampleable_duplicate_exposure_representative(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        {
            "key": "blocked-high-rr",
            "target": 104.0,
            "quality": 95.0,
            "blockers": ["parent_trend_conflict"],
        },
        {
            "key": "sampleable-lower-rr",
            "target": 102.0,
            "quality": 82.0,
            "blockers": [],
        },
    ]
    for row in rows:
        session.add(
            DarkflowInteraction(
                interaction_key=f"candidate-sampleable-duplicate-{row['key']}",
                zone_key=f"zone-sampleable-duplicate-{row['key']}",
                source_snapshot_id=f"snapshot-sampleable-duplicate-{row['key']}",
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
                target_price=float(row["target"]),
                invalidation_price=99.0,
                exit_price=float(row["target"]),
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
                        "score": float(row["quality"]),
                        "confirmations": ["official_rule_mapped", "dynamic_darkflow_target"],
                        "blockers": row["blockers"],
                    },
                },
            )
        )
    await session.commit()

    result = await materialize_darkflow_trade_candidates(session, limit=10)
    candidates = (await session.execute(select(TradeCandidate).order_by(TradeCandidate.target_price))).scalars().all()
    representative = next(candidate for candidate in candidates if candidate.status == "shadow_candidate")
    duplicate = next(candidate for candidate in candidates if candidate.status == "entry_plan_retired")

    assert result["duplicate_exposure_count"] == 1
    assert representative.candidate_key.endswith("sampleable-lower-rr")
    assert representative.blockers == []
    assert representative.target_price == 102.0
    assert duplicate.candidate_key.endswith("blocked-high-rr")
    assert duplicate.decision_payload["duplicate_shadow_plan"]["duplicate_of"] == representative.candidate_key


@pytest.mark.asyncio
async def test_trade_candidate_materialize_fetches_deeper_card_pool_for_coverage(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, symbol in enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT"]):
        session.add(
            DarkflowInteraction(
                interaction_key=f"candidate-deeper-pool-{index}",
                zone_key=f"zone-candidate-deeper-pool-{index}",
                source_snapshot_id=f"snapshot-candidate-deeper-pool-{index}",
                symbol=symbol,
                timeframe="short",
                interval="30m",
                indicator="trend_price",
                playbook="pullback_to_cost",
                direction="long",
                interaction_type="wick_pierce_reclaim",
                event_ts=base + timedelta(minutes=index),
                entry_price=100.0 + index,
                stop_price=99.0 + index,
                target_price=102.0 + index,
                invalidation_price=99.0 + index,
                exit_price=102.0 + index,
                exit_ts=base + timedelta(minutes=30 + index),
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
    await session.commit()

    result = await materialize_darkflow_trade_candidates(session, limit=1)
    candidates = (await session.execute(select(TradeCandidate))).scalars().all()

    assert result["requested_limit"] == 1
    assert result["card_fetch_limit"] > result["requested_limit"]
    assert result["inserted"] == 3
    assert {candidate.symbol for candidate in candidates} == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}


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
async def test_refresh_audits_blocked_candidates_without_promoting_to_shadow(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-blocked-audit",
            zone_key="zone-candidate-blocked-audit",
            source_snapshot_id="snapshot-candidate-blocked-audit",
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
                "quality": {"score": 40.0, "confirmations": ["official_rule_mapped"], "blockers": []},
            },
        )
    )
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=base, created_at=base))
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)

    result = await refresh_darkflow_candidate_promotion(
        session,
        limit=10,
        shadow_limit=10,
        max_candidate_age_hours=0,
        entry_tolerance_pct=0.05,
    )
    candidate = await session.scalar(select(TradeCandidate))
    trades = (await session.execute(select(ShadowPaperTrade))).scalars().all()

    assert result["audit"]["audited"] == 1
    assert result["audit"]["passed"] == 1
    assert result["shadow_forward"]["opened"] == []
    assert candidate is not None
    assert candidate.status == "research_blocked"
    assert candidate.anti_repaint_status == "passed"
    assert candidate.promotion_status == "blocked"
    assert "anti_repaint_audit_missing" not in candidate.promotion_blockers
    assert "quality_score_below_threshold" in candidate.blockers
    assert result["summary"]["gate_samples"][0]["gate_status"] == "blocked"
    assert result["summary"]["gate_samples"][0]["primary_blocker"] == "quality_score_below_threshold"
    assert trades == []


@pytest.mark.asyncio
async def test_refresh_prioritizes_pending_shadow_candidates_before_newer_blocked_rows(session) -> None:
    old_base = datetime.now(timezone.utc) - timedelta(hours=6)
    new_base = datetime.now(timezone.utc) - timedelta(minutes=5)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-old-pending-shadow-audit",
            zone_key="zone-old-pending-shadow-audit",
            source_snapshot_id="snapshot-old-pending-shadow-audit",
            symbol="BTCUSDT",
            timeframe="short",
            interval="30m",
            indicator="trend_price",
            playbook="pullback_to_cost",
            direction="long",
            interaction_type="wick_pierce_reclaim",
            event_ts=old_base,
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
            invalidation_price=99.0,
            exit_price=102.0,
            exit_ts=old_base + timedelta(minutes=30),
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
        DarkflowInteraction(
            interaction_key="candidate-new-blocked-audit-window",
            zone_key="zone-new-blocked-audit-window",
            source_snapshot_id="snapshot-new-blocked-audit-window",
            symbol="ETHUSDT",
            timeframe="short",
            interval="30m",
            indicator="trend_price",
            playbook="pullback_to_cost",
            direction="long",
            interaction_type="wick_pierce_reclaim",
            event_ts=new_base,
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
            invalidation_price=99.0,
            exit_price=102.0,
            exit_ts=new_base + timedelta(minutes=30),
            exit_reason="target_hit",
            pnl_pct=0.02,
            r_multiple=2.0,
            mfe=0.025,
            mae=-0.004,
            status="backtested",
            context={
                "interaction_schema": "v2",
                "quality": {"score": 92.0, "confirmations": ["official_rule_mapped"], "blockers": ["parent_trend_conflict"]},
            },
        )
    )
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)

    result = await audit_darkflow_trade_candidates(session, limit=1, include_blocked=True)
    candidates = (await session.execute(select(TradeCandidate))).scalars().all()
    pending = next(candidate for candidate in candidates if candidate.symbol == "BTCUSDT")
    blocked = next(candidate for candidate in candidates if candidate.symbol == "ETHUSDT")

    assert result["audited"] == 1
    assert result["rows"][0]["candidate_key"] == pending.candidate_key
    assert pending.anti_repaint_status == "passed"
    assert pending.promotion_status == "shadow_forward_pending"
    assert blocked.anti_repaint_status == "missing"


@pytest.mark.asyncio
async def test_refresh_materializes_latest_candidates_before_promotion(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-refresh-materializes-latest",
            zone_key="zone-refresh-materializes-latest",
            source_snapshot_id="snapshot-refresh-materializes-latest",
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
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=base, created_at=base))
    await session.commit()

    result = await refresh_darkflow_candidate_promotion(
        session,
        limit=10,
        shadow_limit=10,
        max_candidate_age_hours=0,
        entry_tolerance_pct=0.05,
    )
    candidate = await session.scalar(select(TradeCandidate))
    trades = (await session.execute(select(ShadowPaperTrade))).scalars().all()

    assert result["materialize"]["inserted"] == 1
    assert result["audit"]["audited"] == 1
    assert len(result["shadow_forward"]["opened"]) == 1
    assert candidate is not None
    assert candidate.shadow_status == "collecting"
    assert len(trades) == 1


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
    candidates = (await session.execute(select(TradeCandidate))).scalars().all()
    now = datetime.now(timezone.utc)
    for index, candidate in enumerate(candidates):
        payload = dict(candidate.decision_payload or {})
        plan = dict(payload.get("entry_plan") or {})
        if candidate.symbol == "BTCUSDT":
            candidate.setup_time = now - timedelta(hours=1)
            plan["valid_until"] = (now + timedelta(hours=1)).isoformat()
        else:
            candidate.setup_time = now + timedelta(minutes=index)
            plan["valid_until"] = (now - timedelta(minutes=1)).isoformat()
        payload["entry_plan"] = plan
        candidate.decision_payload = payload
    await session.commit()

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
async def test_shadow_forward_scans_past_expired_front_rows_to_open_later_candidate(session) -> None:
    fresh_base = datetime.now(timezone.utc) - timedelta(minutes=5)
    expired_base = datetime.now(timezone.utc) - timedelta(hours=8)
    for index in range(3):
        session.add(
            DarkflowInteraction(
                interaction_key=f"candidate-expired-front-{index}",
                zone_key=f"zone-expired-front-{index}",
                source_snapshot_id=f"snapshot-expired-front-{index}",
                symbol=f"ETH{index}USDT",
                timeframe="short",
                interval="30m",
                indicator="trend_price",
                playbook="pullback_to_cost",
                direction="long",
                interaction_type="wick_pierce_reclaim",
                event_ts=expired_base + timedelta(minutes=index),
                entry_price=100.0,
                stop_price=99.0,
                target_price=102.0,
                invalidation_price=99.0,
                exit_price=102.0,
                exit_ts=expired_base + timedelta(minutes=30 + index),
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
                symbol=f"ETH{index}USDT",
                price=100.0,
                raw_payload={},
                collected_at=fresh_base,
                created_at=fresh_base,
            )
        )
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-later-openable",
            zone_key="zone-candidate-later-openable",
            source_snapshot_id="snapshot-candidate-later-openable",
            symbol="BTCUSDT",
            timeframe="short",
            interval="30m",
            indicator="trend_price",
            playbook="pullback_to_cost",
            direction="long",
            interaction_type="wick_pierce_reclaim",
            event_ts=fresh_base,
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
            invalidation_price=99.0,
            exit_price=102.0,
            exit_ts=fresh_base + timedelta(minutes=30),
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
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=fresh_base, created_at=fresh_base))
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)
    await audit_darkflow_trade_candidates(session, limit=10)
    candidates = (await session.execute(select(TradeCandidate))).scalars().all()
    now = datetime.now(timezone.utc)
    for index, candidate in enumerate(candidates):
        payload = dict(candidate.decision_payload or {})
        plan = dict(payload.get("entry_plan") or {})
        if candidate.symbol == "BTCUSDT":
            candidate.setup_time = now - timedelta(hours=1)
            plan["valid_until"] = (now + timedelta(hours=1)).isoformat()
        else:
            candidate.setup_time = now + timedelta(minutes=index)
            plan["valid_until"] = (now - timedelta(minutes=1)).isoformat()
        payload["entry_plan"] = plan
        candidate.decision_payload = payload
    await session.commit()

    result = await open_darkflow_shadow_forward_samples(
        session,
        limit=1,
        max_candidate_age_hours=0,
        entry_tolerance_pct=0.05,
    )
    trades = (await session.execute(select(ShadowPaperTrade))).scalars().all()

    assert result["requested_limit"] == 1
    assert result["scan_limit"] > 1
    assert result["scanned"] > 1
    assert [item["symbol"] for item in result["opened"]] == ["BTCUSDT"]
    assert len(trades) == 1
    assert trades[0].symbol == "BTCUSDT"


@pytest.mark.asyncio
async def test_shadow_forward_skips_candidate_when_latest_price_is_stale(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    stale_price_at = datetime.now(timezone.utc) - timedelta(minutes=45)
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-stale-price-shadow-forward",
            zone_key="zone-stale-price-shadow-forward",
            source_snapshot_id="snapshot-stale-price-shadow-forward",
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
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=stale_price_at, created_at=stale_price_at))
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
    assert result["skipped_count"] == 1
    assert result["skip_reason_counts"] == {"latest_price_stale": 1}
    assert result["skipped"][0]["price_age_seconds"] >= 30 * 60
    assert trades == []


@pytest.mark.asyncio
async def test_shadow_forward_round_robins_markets_for_sample_coverage(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    rows = [
        ("BTCUSDT", "long", 100.0, 99.0, 102.0),
        ("BTCUSDT", "long", 100.2, 99.2, 102.2),
        ("BTCUSDT", "long", 100.4, 99.4, 102.4),
        ("ETHUSDT", "long", 100.0, 99.0, 102.0),
    ]
    for index, (symbol, direction, entry, stop, target) in enumerate(rows):
        session.add(
            DarkflowInteraction(
                interaction_key=f"candidate-round-robin-{index}",
                zone_key=f"zone-round-robin-{index}",
                source_snapshot_id=f"snapshot-round-robin-{index}",
                symbol=symbol,
                timeframe="short",
                interval="30m",
                indicator="trend_price",
                playbook="pullback_to_cost",
                direction=direction,
                interaction_type="wick_pierce_reclaim",
                event_ts=base + timedelta(seconds=index),
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                invalidation_price=stop,
                exit_price=target,
                exit_ts=base + timedelta(minutes=30 + index),
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
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=base, created_at=base))
    session.add(PriceSnapshot(symbol="ETHUSDT", price=100.0, raw_payload={}, collected_at=base, created_at=base))
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)
    await audit_darkflow_trade_candidates(session, limit=10)

    result = await open_darkflow_shadow_forward_samples(
        session,
        limit=2,
        max_candidate_age_hours=0,
        entry_tolerance_pct=0.05,
    )

    assert len(result["opened"]) == 2
    assert {item["symbol"] for item in result["opened"]} == {"BTCUSDT", "ETHUSDT"}


@pytest.mark.asyncio
async def test_shadow_forward_caps_open_samples_per_symbol_direction(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    for index in range(3):
        session.add(
            ShadowPaperTrade(
                strategy_name=DARKFLOW_V2_SHADOW_STRATEGY_NAME,
                candidate_type="trade_candidate",
                candidate_key=f"existing-btc-{index}",
                signal_key=f"existing-btc-signal-{index}",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0 + index,
                stop_loss=99.0 + index,
                take_profit=102.0 + index,
                position_size=1.0,
                status="open",
                opened_at=base + timedelta(seconds=index),
                context={
                    "shadow_forward": True,
                    "shadow_plan_fingerprint": f"existing-btc-plan-{index}",
                    "candidate_snapshot": {
                        "strategy_id": "pullback_to_cost",
                        "timeframe": "short",
                        "entry_price": 100.0 + index,
                        "stop_price": 99.0 + index,
                        "target_price": 102.0 + index,
                    },
                },
            )
        )
    for index, symbol in enumerate(["BTCUSDT", "ETHUSDT"]):
        session.add(
            DarkflowInteraction(
                interaction_key=f"candidate-market-cap-{symbol}",
                zone_key=f"zone-market-cap-{symbol}",
                source_snapshot_id=f"snapshot-market-cap-{symbol}",
                symbol=symbol,
                timeframe="short",
                interval="30m",
                indicator="trend_price",
                playbook="pullback_to_cost",
                direction="long",
                interaction_type="wick_pierce_reclaim",
                event_ts=base + timedelta(minutes=index),
                entry_price=100.0,
                stop_price=99.0,
                target_price=102.0,
                invalidation_price=99.0,
                exit_price=102.0,
                exit_ts=base + timedelta(minutes=30 + index),
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
        session.add(PriceSnapshot(symbol=symbol, price=100.0, raw_payload={}, collected_at=base, created_at=base))
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)
    await audit_darkflow_trade_candidates(session, limit=10)

    result = await open_darkflow_shadow_forward_samples(
        session,
        limit=2,
        max_candidate_age_hours=0,
        entry_tolerance_pct=0.05,
    )

    assert [item["symbol"] for item in result["opened"]] == ["ETHUSDT"]
    assert any(item["reason"] == "market_shadow_forward_slot_full" and item["symbol"] == "BTCUSDT" for item in result["skipped"])


@pytest.mark.asyncio
async def test_shadow_forward_allows_one_extra_strategy_diversity_sample_per_market(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    for index in range(3):
        session.add(
            ShadowPaperTrade(
                strategy_name=DARKFLOW_V2_SHADOW_STRATEGY_NAME,
                candidate_type="trade_candidate",
                candidate_key=f"existing-btc-pullback-{index}",
                signal_key=f"existing-btc-pullback-signal-{index}",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0 + index,
                stop_loss=99.0 + index,
                take_profit=102.0 + index,
                position_size=1.0,
                status="open",
                opened_at=base + timedelta(seconds=index),
                context={
                    "shadow_forward": True,
                    "shadow_plan_fingerprint": f"existing-btc-pullback-plan-{index}",
                    "candidate_snapshot": {
                        "strategy_id": "pullback_to_cost",
                        "timeframe": "short",
                        "entry_price": 100.0 + index,
                        "stop_price": 99.0 + index,
                        "target_price": 102.0 + index,
                    },
                },
            )
        )
    for index, playbook in enumerate(["pullback_to_cost", "liquidity_sweep_reversal", "trend_ride_extension"]):
        session.add(
            DarkflowInteraction(
                interaction_key=f"candidate-diversity-{playbook}",
                zone_key=f"zone-diversity-{playbook}",
                source_snapshot_id=f"snapshot-diversity-{playbook}",
                symbol="BTCUSDT",
                timeframe="short",
                interval="30m",
                indicator="trend_price",
                playbook=playbook,
                direction="long",
                interaction_type="wick_pierce_reclaim",
                event_ts=base + timedelta(seconds=index),
                entry_price=100.0 + index * 0.05,
                stop_price=99.0,
                target_price=102.0 + index * 0.05,
                invalidation_price=99.0,
                exit_price=102.0,
                exit_ts=base + timedelta(minutes=30 + index),
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
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=base, created_at=base))
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)
    await audit_darkflow_trade_candidates(session, limit=10)

    result = await open_darkflow_shadow_forward_samples(
        session,
        limit=3,
        max_candidate_age_hours=0,
        entry_tolerance_pct=0.05,
    )
    opened = (await session.scalars(select(ShadowPaperTrade).where(ShadowPaperTrade.status == "open"))).all()

    opened_strategy_ids = {item["strategy_id"] for item in result["opened"]}
    skipped_strategy_ids = {
        item["strategy_id"]
        for item in result["skipped"]
        if item.get("reason") == "market_shadow_forward_slot_full" and item.get("symbol") == "BTCUSDT"
    }

    assert len(result["opened"]) == 1
    assert opened_strategy_ids <= {"liquidity_sweep_reversal", "trend_ride_extension"}
    assert len(opened_strategy_ids) == 1
    assert result["opened"][0]["market_diversity_slot"] is True
    assert result["opened"][0]["open_market_trades"] == 4
    assert sum(1 for trade in opened if trade.symbol == "BTCUSDT" and trade.direction == "long") == 4
    assert any(
        item["reason"] == "market_shadow_forward_slot_full"
        and item["symbol"] == "BTCUSDT"
        and item["strategy_id"] == "pullback_to_cost"
        for item in result["skipped"]
    )
    assert skipped_strategy_ids >= ({"liquidity_sweep_reversal", "trend_ride_extension"} - opened_strategy_ids)


@pytest.mark.asyncio
async def test_shadow_forward_retires_duplicate_open_plan(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    for index in range(2):
        session.add(
            DarkflowInteraction(
                interaction_key=f"candidate-duplicate-plan-{index}",
                zone_key=f"zone-candidate-duplicate-plan-{index}",
                source_snapshot_id=f"snapshot-candidate-duplicate-plan-{index}",
                symbol="BTCUSDT",
                timeframe="short",
                interval="30m",
                indicator="trend_price",
                playbook="pullback_to_cost",
                direction="long",
                interaction_type="wick_pierce_reclaim",
                event_ts=base + timedelta(seconds=index),
                entry_price=100.0 + index * 0.01,
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
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=base, created_at=base))
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
    candidates = (await session.execute(select(TradeCandidate).order_by(TradeCandidate.shadow_status))).scalars().all()

    assert len(result["opened"]) == 1
    assert len(trades) == 1
    assert trades[0].context["shadow_plan_fingerprint"]
    assert any(item["reason"] == "duplicate_shadow_forward_plan" for item in result["updated"])
    assert {candidate.shadow_status for candidate in candidates} == {"collecting", "retired"}
    retired = next(candidate for candidate in candidates if candidate.shadow_status == "retired")
    assert retired.promotion_status == "duplicate_shadow_plan"
    assert retired.promotion_blockers == ["duplicate_shadow_forward_plan"]


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
    candidate = await session.scalar(select(TradeCandidate))

    assert result["opened"] == []
    assert len(trades) == 0
    assert result["skipped"][0]["reason"] == "entry_plan_missed"
    assert result["skipped"][0]["entry_plan_state"]["reason"] == "entry_range_missed"
    assert result["updated"][0]["reason"] == "entry_plan_missed"
    assert candidate is not None
    assert candidate.status == "entry_plan_retired"
    assert candidate.shadow_status == "retired"
    assert candidate.promotion_status == "entry_plan_retired"
    assert "entry_plan_retired" in candidate.promotion_blockers
    assert candidate.decision_payload["entry_plan_retirement"]["reason"] == "entry_plan_missed"

    await materialize_darkflow_trade_candidates(session, limit=10)
    await session.refresh(candidate)

    assert candidate.status == "entry_plan_retired"
    assert candidate.shadow_status == "retired"
    assert candidate.promotion_status == "entry_plan_retired"


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
async def test_shadow_forward_pauses_weak_symbol_direction_market(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    for index, pnl in enumerate([-0.01, -0.02, -0.015]):
        session.add(
            ShadowPaperTrade(
                strategy_name=DARKFLOW_V2_SHADOW_STRATEGY_NAME,
                candidate_type="trade_candidate",
                candidate_key=f"old-doge-{index}",
                signal_key=f"old-doge-signal-{index}",
                symbol="DOGEUSDT",
                timeframe="short",
                direction="short",
                entry_price=0.11,
                stop_loss=0.112,
                take_profit=0.106,
                position_size=1.0,
                status="closed",
                pnl=pnl,
                opened_at=base - timedelta(hours=2, minutes=index),
                closed_at=base - timedelta(hours=1, minutes=index),
                context={
                    "shadow_plan_fingerprint": f"old-doge-plan-{index}",
                    "candidate_snapshot": {"strategy_id": "pullback_to_cost"},
                },
            )
        )
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-shadow-weak-market",
            zone_key="zone-candidate-shadow-weak-market",
            source_snapshot_id="snapshot-candidate-shadow-weak-market",
            symbol="DOGEUSDT",
            timeframe="short",
            interval="30m",
            indicator="trend_price",
            playbook="pullback_to_cost",
            direction="short",
            interaction_type="wick_pierce_reclaim",
            event_ts=base,
            entry_price=0.11,
            stop_price=0.112,
            target_price=0.106,
            invalidation_price=0.112,
            exit_price=0.106,
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
    session.add(PriceSnapshot(symbol="DOGEUSDT", price=0.11, raw_payload={}, collected_at=base, created_at=base))
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)
    await audit_darkflow_trade_candidates(session, limit=10)

    result = await open_darkflow_shadow_forward_samples(
        session,
        limit=10,
        max_candidate_age_hours=1,
        entry_tolerance_pct=0.05,
    )
    candidate = await session.scalar(select(TradeCandidate).where(TradeCandidate.symbol == "DOGEUSDT"))
    open_trades = (await session.execute(select(ShadowPaperTrade).where(ShadowPaperTrade.status == "open"))).scalars().all()

    assert result["opened"] == []
    assert result["skipped"][0]["reason"] == "shadow_market_performance_paused"
    assert result["skipped"][0]["market_gate"]["decision"] == "paused"
    assert result["skipped"][0]["market_gate"]["strategy_id"] == "pullback_to_cost"
    assert open_trades == []
    assert candidate is not None
    assert candidate.promotion_status == "shadow_market_paused"
    assert "shadow_market_performance_paused" in candidate.promotion_blockers
    assert candidate.decision_payload["shadow_market_gate"]["reason"] == "weak_symbol_direction_strategy_shadow_performance"


@pytest.mark.asyncio
async def test_shadow_forward_does_not_pause_different_strategy_for_weak_market_peer(session) -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    for index, pnl in enumerate([-0.01, -0.02, -0.015]):
        session.add(
            ShadowPaperTrade(
                strategy_name=DARKFLOW_V2_SHADOW_STRATEGY_NAME,
                candidate_type="trade_candidate",
                candidate_key=f"old-doge-peer-{index}",
                signal_key=f"old-doge-peer-signal-{index}",
                symbol="DOGEUSDT",
                timeframe="short",
                direction="short",
                entry_price=0.11,
                stop_loss=0.112,
                take_profit=0.106,
                position_size=1.0,
                status="closed",
                pnl=pnl,
                opened_at=base - timedelta(hours=2, minutes=index),
                closed_at=base - timedelta(hours=1, minutes=index),
                context={
                    "shadow_plan_fingerprint": f"old-doge-peer-plan-{index}",
                    "candidate_snapshot": {"strategy_id": "pullback_to_cost"},
                },
            )
        )
    session.add(
        DarkflowInteraction(
            interaction_key="candidate-shadow-weak-peer-strategy",
            zone_key="zone-candidate-shadow-weak-peer-strategy",
            source_snapshot_id="snapshot-candidate-shadow-weak-peer-strategy",
            symbol="DOGEUSDT",
            timeframe="short",
            interval="30m",
            indicator="liquidity_sweep",
            playbook="liquidity_sweep_reversal",
            direction="short",
            interaction_type="first_touch",
            event_ts=base,
            entry_price=0.11,
            stop_price=0.112,
            target_price=0.106,
            invalidation_price=0.112,
            exit_price=0.106,
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
    session.add(PriceSnapshot(symbol="DOGEUSDT", price=0.11, raw_payload={}, collected_at=base, created_at=base))
    await session.commit()
    await materialize_darkflow_trade_candidates(session, limit=10)
    await audit_darkflow_trade_candidates(session, limit=10)

    result = await open_darkflow_shadow_forward_samples(
        session,
        limit=10,
        max_candidate_age_hours=1,
        entry_tolerance_pct=0.05,
    )
    candidate = await session.scalar(select(TradeCandidate).where(TradeCandidate.symbol == "DOGEUSDT"))
    open_trades = (await session.execute(select(ShadowPaperTrade).where(ShadowPaperTrade.status == "open"))).scalars().all()

    assert result["skipped"] == []
    assert len(result["opened"]) == 1
    assert result["opened"][0]["strategy_id"] == "liquidity_sweep_reversal"
    assert len(open_trades) == 1
    assert candidate is not None
    assert candidate.promotion_status == "shadow_forward_collecting"


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
    session.add(
        ExperimentRun(
            name="darkflow_pipeline_run",
            status="running",
            scope={"lineage": "core_darkflow_v2"},
            params={},
            metrics={"stage": "started"},
            notes="test running heartbeat",
            created_at=base + timedelta(minutes=5),
        )
    )
    await session.commit()

    report = await darkflow_entry_plan_state_report(session, limit=10, entry_tolerance_pct=0.05)

    assert report["candidate_count"] == 4
    assert report["state_counts"] == {"waiting": 1, "missed": 1, "triggered": 1, "invalidated": 1}
    assert report["freshness"]["status"] == "fresh"
    assert report["freshness"]["latest_pipeline_run_at"] is not None
    assert report["freshness"]["opportunity_status"] == "active"
    assert report["freshness"]["pipeline"]["expected_worker"] == "darkflow-worker"
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
