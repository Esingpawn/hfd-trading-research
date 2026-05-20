from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.domain.trade_candidates.lifecycle import PROMOTION_BLOCKER_ANTI_REPAINT_MISSING
from app.models import PriceSnapshot, ShadowPaperTrade, TradeCandidate
from app.services.darkflow_candidate_promotion import DARKFLOW_V2_SHADOW_STRATEGY_NAME, darkflow_candidate_promotion_report


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
async def test_promotion_report_adds_read_only_gate_counts_and_samples(session) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        _candidate(
            "gate-review-ready",
            symbol="BTCUSDT",
            setup_time=now,
            promotion_status="paper_review_ready",
            anti_repaint_status="passed",
            shadow_status="passed",
        ),
        _candidate(
            "gate-watching-entry",
            symbol="ETHUSDT",
            setup_time=now - timedelta(minutes=1),
            promotion_status="paper_review_ready",
            anti_repaint_status="passed",
            shadow_status="passed",
        ),
        _candidate(
            "gate-blocked-anti-repaint",
            symbol="SOLUSDT",
            setup_time=now - timedelta(minutes=2),
            promotion_status="anti_repaint_pending",
            anti_repaint_status="missing",
            shadow_status="not_started",
            promotion_blockers=[PROMOTION_BLOCKER_ANTI_REPAINT_MISSING],
        ),
    ]
    session.add_all(rows)
    session.add_all(
        [
            PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=now, created_at=now),
            PriceSnapshot(symbol="ETHUSDT", price=99.2, raw_payload={}, collected_at=now, created_at=now),
            PriceSnapshot(symbol="SOLUSDT", price=100.0, raw_payload={}, collected_at=now, created_at=now),
            ShadowPaperTrade(
                strategy_name=DARKFLOW_V2_SHADOW_STRATEGY_NAME,
                candidate_type="trade_candidate",
                candidate_key="gate-review-ready",
                signal_key="gate-review-ready-shadow-1",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=102.0,
                position_size=1.0,
                status="closed",
                pnl=0.02,
                opened_at=now - timedelta(minutes=30),
                closed_at=now - timedelta(minutes=10),
                context={},
            ),
        ]
    )
    await session.commit()

    report = await darkflow_candidate_promotion_report(session, limit=10)

    assert report["candidate_count"] == 3
    assert report["promotion_status_counts"] == {"paper_review_ready": 2, "anti_repaint_pending": 1}
    assert report["gate_status_counts"] == {"review_ready": 1, "watching_entry": 1, "blocked": 1}
    assert report["policy"]["report_only"] is True
    assert report["policy"]["opens_paper_trades"] is False
    assert report["policy"]["opens_live_orders"] is False
    assert report["policy"]["max_gate_status"] == "review_ready"

    by_key = {item["candidate_key"]: item for item in report["gate_samples"]}
    assert by_key["gate-review-ready"]["gate_status"] == "review_ready"
    assert by_key["gate-review-ready"]["evidence_summary"]["shadow_closed_trades"] == 1
    assert by_key["gate-watching-entry"]["gate_status"] == "watching_entry"
    assert by_key["gate-watching-entry"]["primary_blocker"] == "entry_plan_waiting"
    assert by_key["gate-blocked-anti-repaint"]["blocker_groups"]["anti_repaint"][0]["severity"] == "blocker"
    assert report["samples"][0]["promotion_gate"]["gate_status"] == "review_ready"


@pytest.mark.asyncio
async def test_promotion_report_shadow_stats_exclude_invalid_closed_outcomes_from_performance(session) -> None:
    now = datetime.now(timezone.utc)
    candidate = _candidate(
        "gate-invalid-shadow-stats",
        symbol="BTCUSDT",
        setup_time=now,
        promotion_status="shadow_forward_collecting",
        anti_repaint_status="passed",
        shadow_status="collecting",
    )
    session.add(candidate)
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=now, created_at=now))
    session.add_all(
        [
            ShadowPaperTrade(
                strategy_name=DARKFLOW_V2_SHADOW_STRATEGY_NAME,
                candidate_type="trade_candidate",
                candidate_key=candidate.candidate_key,
                signal_key="gate-invalid-shadow-valid",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=102.0,
                position_size=1.0,
                status="closed",
                pnl=0.02,
                opened_at=now - timedelta(minutes=40),
                closed_at=now - timedelta(minutes=20),
                context={},
            ),
            ShadowPaperTrade(
                strategy_name=DARKFLOW_V2_SHADOW_STRATEGY_NAME,
                candidate_type="trade_candidate",
                candidate_key=candidate.candidate_key,
                signal_key="gate-invalid-shadow-invalid",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=102.0,
                position_size=1.0,
                status="closed",
                pnl=None,
                opened_at=now - timedelta(minutes=35),
                closed_at=now - timedelta(minutes=15),
                context={},
            ),
        ]
    )
    await session.commit()

    report = await darkflow_candidate_promotion_report(session, limit=10)
    sample = report["samples"][0]

    assert sample["shadow_stats"]["closed_trades"] == 2
    assert sample["shadow_stats"]["valid_outcome_trades"] == 1
    assert sample["shadow_stats"]["invalid_outcome_trades"] == 1
    assert sample["shadow_stats"]["avg_pnl"] == pytest.approx(0.02)
    assert sample["shadow_stats"]["win_rate"] == 1.0


@pytest.mark.asyncio
async def test_promotion_report_includes_setup_expectancy_gate_evidence(session) -> None:
    now = datetime.now(timezone.utc)
    candidate = _candidate(
        "gate-weak-setup-expectancy",
        symbol="BTCUSDT",
        setup_time=now,
        promotion_status="paper_review_ready",
        anti_repaint_status="passed",
        shadow_status="passed",
    )
    session.add(candidate)
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=now, created_at=now))
    session.add_all(
        [
            ShadowPaperTrade(
                strategy_name=DARKFLOW_V2_SHADOW_STRATEGY_NAME,
                candidate_type="trade_candidate",
                candidate_key=f"weak-peer-{index}",
                signal_key=f"weak-peer-{index}",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=102.0,
                position_size=1.0,
                status="closed",
                pnl=-0.02,
                exit_reason="stop_loss",
                opened_at=now - timedelta(minutes=60 + index),
                closed_at=now - timedelta(minutes=30 + index),
                context={
                    "candidate_snapshot": {
                        "strategy_family": "darkflow_v2",
                        "setup_type": "wick_pierce_reclaim",
                        "strategy_id": "pullback_to_cost",
                        "strategy_name": "Pullback To Cost",
                        "timeframe": "short",
                        "market_state": "trend_pullback",
                    },
                    "shadow_plan_fingerprint": f"weak-peer-{index}",
                },
            )
            for index in range(6)
        ]
    )
    await session.commit()

    report = await darkflow_candidate_promotion_report(session, limit=10)
    gate = report["gate_samples"][0]

    assert gate["gate_status"] == "blocked"
    assert gate["primary_blocker"] == "setup_expectancy_blacklist"
    assert gate["blocker_groups"]["setup_expectancy"][0]["severity"] == "blocker"
    assert gate["evidence_summary"]["setup_expectancy"]["classification"] == "blacklist"
    assert gate["evidence_summary"]["setup_expectancy"]["sample_count"] == 6
    assert report["samples"][0]["setup_expectancy"]["profit_factor"] == pytest.approx(0.0)


def _candidate(
    candidate_key: str,
    *,
    symbol: str,
    setup_time: datetime,
    promotion_status: str,
    anti_repaint_status: str,
    shadow_status: str,
    promotion_blockers: list[str] | None = None,
) -> TradeCandidate:
    valid_until = setup_time + timedelta(hours=2)
    return TradeCandidate(
        candidate_key=candidate_key,
        source_type="darkflow_interaction",
        source_interaction_id=None,
        lineage="core_darkflow_v2",
        strategy_family="darkflow_v2",
        strategy_id="pullback_to_cost",
        strategy_name="Pullback To Cost",
        symbol=symbol,
        timeframe="short",
        interval="30m",
        direction="long",
        setup_type="wick_pierce_reclaim",
        market_state="trend_pullback",
        setup_time=setup_time,
        entry_price=100.0,
        stop_price=99.0,
        target_price=102.0,
        rr_ratio=2.0,
        quality_score=90.0,
        rule_score=90.0,
        status="shadow_candidate",
        promotion_status=promotion_status,
        anti_repaint_status=anti_repaint_status,
        shadow_status=shadow_status,
        paper_eligible=False,
        live_eligible=False,
        blockers=[],
        promotion_blockers=promotion_blockers or [],
        supporting_signals=[],
        decision_payload={
            "entry_plan": {
                "plan_type": "frozen_darkflow_v2_entry_plan",
                "planned_entry": 100.0,
                "planned_stop": 99.0,
                "take_profit_levels": [{"label": "TP1", "price": 102.0}],
                "invalidation_price": 99.0,
                "entry_range": {"lower": 99.4, "upper": 100.6, "source": "entry_reference_tolerance"},
                "valid_until": valid_until.isoformat(),
            }
        },
        materialized_at=setup_time,
        updated_at=setup_time,
    )
