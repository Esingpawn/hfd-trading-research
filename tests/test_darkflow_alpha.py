from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import DarkflowInteraction, PaperTrade, PriceSnapshot, ShadowPaperTrade, TradeCandidate
from app.services.darkflow_alpha import accelerate_darkflow_alpha, darkflow_alpha_scoreboard
from app.services.darkflow_candidate_promotion import DARKFLOW_V2_SHADOW_STRATEGY_NAME


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
async def test_darkflow_alpha_scoreboard_groups_shadow_forward_by_playbook_symbol_direction(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add_all(
        [
            _shadow_trade(
                "pullback-win-1",
                candidate_key="candidate-pullback-1",
                symbol="BTCUSDT",
                direction="long",
                strategy_id="pullback_to_cost",
                market_state="trend_pullback",
                pnl=0.03,
                opened_at=base,
            ),
            _shadow_trade(
                "pullback-win-duplicate",
                candidate_key="candidate-pullback-dup",
                symbol="BTCUSDT",
                direction="long",
                strategy_id="pullback_to_cost",
                market_state="trend_pullback",
                pnl=-0.01,
                opened_at=base + timedelta(minutes=5),
                plan_fingerprint="pullback:BTCUSDT:long:100:99:103",
            ),
            _shadow_trade(
                "pullback-win-dedup-source",
                candidate_key="candidate-pullback-source",
                symbol="BTCUSDT",
                direction="long",
                strategy_id="pullback_to_cost",
                market_state="trend_pullback",
                pnl=0.02,
                opened_at=base + timedelta(minutes=10),
                plan_fingerprint="pullback:BTCUSDT:long:100:99:103",
            ),
            _shadow_trade(
                "sweep-loss-1",
                candidate_key="candidate-sweep-1",
                symbol="ETHUSDT",
                direction="short",
                strategy_id="liquidity_sweep_reversal",
                market_state="sweep_reversal",
                pnl=-0.02,
                opened_at=base + timedelta(hours=1),
            ),
            ShadowPaperTrade(
                strategy_name="shadow_feature_candidates_v1",
                candidate_type="segment_candidate",
                candidate_key="legacy-candidate",
                signal_key="legacy-signal",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=103.0,
                position_size=1.0,
                status="closed",
                pnl=1.0,
                opened_at=base,
                closed_at=base + timedelta(minutes=1),
                context={},
            ),
        ]
    )
    await session.commit()

    report = await darkflow_alpha_scoreboard(session, limit=10, min_closed_trades=1)

    assert report["lineage"] == "core_darkflow_v2"
    assert report["strategy_name"] == DARKFLOW_V2_SHADOW_STRATEGY_NAME
    assert report["policy"]["report_only"] is True
    assert report["policy"]["opens_paper_trades"] is False
    assert report["policy"]["opens_live_orders"] is False
    assert report["totals"]["source_trade_count"] == 4
    assert report["totals"]["duplicate_trade_count"] == 1

    by_key = {(row["strategy_id"], row["symbol"], row["direction"]): row for row in report["rows"]}
    pullback = by_key[("pullback_to_cost", "BTCUSDT", "long")]
    assert pullback["closed_trades"] == 2
    assert pullback["source_trade_count"] == 3
    assert pullback["duplicate_trade_count"] == 1
    assert pullback["win_rate"] == 1.0
    assert pullback["profit_factor"] == 999.0
    assert pullback["conclusion"] == "样本收集中"
    assert "继续积累" in pullback["next_action"]

    sweep = by_key[("liquidity_sweep_reversal", "ETHUSDT", "short")]
    assert sweep["closed_trades"] == 1
    assert sweep["win_rate"] == 0.0
    assert sweep["conclusion"] == "观察名单"


@pytest.mark.asyncio
async def test_accelerate_darkflow_alpha_opens_only_isolated_shadow_forward_samples(session) -> None:
    now = datetime.now(timezone.utc)
    source = _source_interaction(setup_time=now - timedelta(minutes=15))
    session.add(source)
    await session.flush()
    session.add(_candidate("darkflow-card:v2:source-triggered", setup_time=now - timedelta(minutes=15), source_interaction_id=source.id))
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=now, created_at=now))
    await session.commit()

    result = await accelerate_darkflow_alpha(
        session,
        candidate_limit=10,
        shadow_limit=5,
        materialize=False,
        mark_first=True,
        entry_tolerance_pct=0.01,
    )

    shadow_rows = (await session.scalars(select(ShadowPaperTrade))).all()
    paper_rows = (await session.scalars(select(PaperTrade))).all()

    assert result["policy"]["uses_shadow_forward_only"] is True
    assert result["policy"]["opens_paper_trades"] is False
    assert result["policy"]["opens_live_orders"] is False
    assert result["steps"]["promotion_refresh"]["shadow_forward"]["opened"][0]["candidate_key"] == "darkflow-card:v2:source-triggered"
    assert len(shadow_rows) == 1
    assert shadow_rows[0].strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME
    assert shadow_rows[0].context["shadow_forward"] is True
    assert shadow_rows[0].context["opens_paper_trades"] is False
    assert shadow_rows[0].context["opens_live_orders"] is False
    assert paper_rows == []


def _shadow_trade(
    signal_key: str,
    *,
    candidate_key: str,
    symbol: str,
    direction: str,
    strategy_id: str,
    market_state: str,
    pnl: float,
    opened_at: datetime,
    plan_fingerprint: str | None = None,
) -> ShadowPaperTrade:
    return ShadowPaperTrade(
        strategy_name=DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        candidate_type="trade_candidate",
        candidate_key=candidate_key,
        signal_key=signal_key,
        symbol=symbol,
        timeframe="short",
        direction=direction,
        entry_price=100.0,
        stop_loss=99.0 if direction == "long" else 101.0,
        take_profit=103.0 if direction == "long" else 97.0,
        position_size=1.0,
        status="closed",
        pnl=pnl,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=30),
        context={
            "horizon": "live",
            "shadow_forward": True,
            "shadow_plan_fingerprint": plan_fingerprint or signal_key,
            "candidate_snapshot": {
                "candidate_key": candidate_key,
                "strategy_id": strategy_id,
                "strategy_name": strategy_id.replace("_", " ").title(),
                "symbol": symbol,
                "timeframe": "short",
                "interval": "30m",
                "direction": direction,
                "setup_type": "first_touch",
                "market_state": market_state,
                "entry_price": 100.0,
                "stop_price": 99.0 if direction == "long" else 101.0,
                "target_price": 103.0 if direction == "long" else 97.0,
                "quality_score": 80.0,
                "rr_ratio": 3.0,
            },
        },
    )


def _candidate(candidate_key: str, *, setup_time: datetime, source_interaction_id: str | None = None) -> TradeCandidate:
    return TradeCandidate(
        candidate_key=candidate_key,
        source_type="darkflow_interaction",
        source_interaction_id=source_interaction_id,
        lineage="core_darkflow_v2",
        strategy_family="darkflow_v2",
        strategy_id="pullback_to_cost",
        strategy_name="Pullback To Cost",
        symbol="BTCUSDT",
        timeframe="short",
        interval="30m",
        direction="long",
        setup_type="first_touch",
        market_state="trend_pullback",
        setup_time=setup_time,
        entry_price=100.0,
        stop_price=99.0,
        target_price=103.0,
        rr_ratio=3.0,
        quality_score=82.0,
        rule_score=8.2,
        status="shadow_candidate",
        promotion_status="shadow_forward_pending",
        anti_repaint_status="passed",
        shadow_status="not_started",
        paper_eligible=False,
        live_eligible=False,
        blockers=[],
        promotion_blockers=[],
        supporting_signals=["trend_price", "inst_vwap"],
        decision_payload={
            "entry_plan": {
                "plan_type": "frozen_darkflow_v2_entry_plan",
                "planned_entry": 100.0,
                "planned_stop": 99.0,
                "take_profit_levels": [{"label": "TP1", "price": 103.0}],
                "invalidation_price": 99.0,
                "entry_range": {"lower": 99.4, "upper": 100.6, "source": "entry_reference_tolerance"},
                "valid_until": (setup_time + timedelta(hours=2)).isoformat(),
            }
        },
        materialized_at=setup_time,
        updated_at=setup_time,
    )


def _source_interaction(*, setup_time: datetime) -> DarkflowInteraction:
    return DarkflowInteraction(
        interaction_key="source-triggered",
        zone_key="zone-source-triggered",
        source_snapshot_id="snapshot-source-triggered",
        symbol="BTCUSDT",
        timeframe="short",
        interval="30m",
        indicator="trend_price",
        playbook="pullback_to_cost",
        direction="long",
        interaction_type="first_touch",
        event_ts=setup_time,
        entry_price=100.0,
        stop_price=99.0,
        target_price=103.0,
        invalidation_price=99.0,
        exit_price=103.0,
        exit_ts=setup_time + timedelta(minutes=30),
        exit_reason="target_hit",
        pnl_pct=0.03,
        r_multiple=3.0,
        mfe=0.035,
        mae=-0.002,
        status="backtested",
        context={
            "interaction_schema": "v2",
            "target_plan": {"model": "tutorial_dynamic_zone_target_v1"},
            "quality": {
                "score": 82.0,
                "confirmations": ["trend_price", "inst_vwap"],
                "blockers": [],
            },
        },
    )
