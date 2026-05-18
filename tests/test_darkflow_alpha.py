from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import DarkflowInteraction, PaperTrade, PriceSnapshot, ShadowPaperTrade, TradeCandidate
from app.services.darkflow_alpha import accelerate_darkflow_alpha, darkflow_alpha_sampling_plan, darkflow_alpha_scoreboard
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
async def test_darkflow_alpha_sampling_plan_prioritizes_strong_groups_and_pauses_weak_groups(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add_all(
        [
            _shadow_trade(
                f"strong-{index}",
                candidate_key=f"strong-candidate-{index}",
                symbol="BTCUSDT",
                direction="long",
                strategy_id="pullback_to_cost",
                market_state="trend_pullback",
                pnl=0.02,
                opened_at=base + timedelta(minutes=index),
            )
            for index in range(3)
        ]
        + [
            _shadow_trade(
                f"weak-{index}",
                candidate_key=f"weak-candidate-{index}",
                symbol="BTCUSDT",
                direction="long",
                strategy_id="liquidity_sweep_reversal",
                market_state="liquidity_hunt_reversal",
                pnl=-0.02,
                opened_at=base + timedelta(hours=1, minutes=index),
            )
            for index in range(3)
        ]
    )
    await session.commit()

    plan = await darkflow_alpha_sampling_plan(session, limit=10)

    priority_keys = {item["group_key"] for item in plan["priority_groups"]}
    paused_keys = {item["group_key"] for item in plan["paused_groups"]}
    assert "pullback_to_cost|BTCUSDT|long|short|trend_pullback" in priority_keys
    assert "liquidity_sweep_reversal|BTCUSDT|long|short|liquidity_hunt_reversal" in paused_keys
    assert plan["policy"]["opens_paper_trades"] is False
    assert plan["policy"]["opens_live_orders"] is False


@pytest.mark.asyncio
async def test_darkflow_alpha_sampling_plan_uses_subportfolio_recommendations(session) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add_all(
        [
            _shadow_trade(
                f"whitelist-{index}",
                candidate_key=f"whitelist-candidate-{index}",
                symbol="HYPEUSDT",
                direction="long",
                strategy_id="liquidity_sweep_reversal",
                market_state="liquidity_hunt_reversal",
                pnl=0.02,
                opened_at=base + timedelta(minutes=index),
            )
            for index in range(6)
        ]
        + [
            _shadow_trade(
                f"blacklist-{index}",
                candidate_key=f"blacklist-candidate-{index}",
                symbol="BTCUSDT",
                direction="long",
                strategy_id="trend_ride_extension",
                market_state="trend_extension",
                pnl=-0.02,
                opened_at=base + timedelta(hours=1, minutes=index),
            )
            for index in range(10)
        ]
    )
    await session.commit()

    plan = await darkflow_alpha_sampling_plan(session, limit=10)

    priority_subportfolios = {item["group_key"] for item in plan["priority_subportfolio_groups"]}
    paused_subportfolios = {item["group_key"] for item in plan["paused_subportfolio_groups"]}
    deweighted_strategies = {item["strategy_id"] for item in plan["deweighted_strategies"]}

    assert "liquidity_sweep_reversal|HYPEUSDT|long|liquidity_hunt_reversal" in priority_subportfolios
    assert "trend_ride_extension|BTCUSDT|long|trend_extension" in paused_subportfolios
    assert "trend_ride_extension" in deweighted_strategies
    assert plan["policy"]["uses_subportfolio_recommendations"] is True


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


@pytest.mark.asyncio
async def test_accelerate_darkflow_alpha_hard_blocks_recommendation_paused_group_before_legacy_exploration(session) -> None:
    now = datetime.now(timezone.utc)
    history_base = now - timedelta(hours=3)
    session.add_all(
        [
            _shadow_trade(
                f"strong-history-{index}",
                candidate_key=f"strong-history-candidate-{index}",
                symbol="BTCUSDT",
                direction="long",
                strategy_id="pullback_to_cost",
                market_state="trend_pullback",
                pnl=0.02,
                opened_at=history_base + timedelta(minutes=index),
            )
            for index in range(3)
        ]
        + [
            _shadow_trade(
                f"weak-history-{index}",
                candidate_key=f"weak-history-candidate-{index}",
                symbol="BTCUSDT",
                direction="long",
                strategy_id="liquidity_sweep_reversal",
                market_state="liquidity_hunt_reversal",
                pnl=-0.02,
                opened_at=history_base + timedelta(minutes=30 + index),
            )
            for index in range(3)
        ]
    )
    strong_source = _source_interaction(
        interaction_key="strong-source",
        playbook="pullback_to_cost",
        setup_time=now - timedelta(minutes=15),
    )
    weak_source = _source_interaction(
        interaction_key="weak-source",
        playbook="liquidity_sweep_reversal",
        setup_time=now - timedelta(minutes=14),
    )
    session.add_all([strong_source, weak_source])
    await session.flush()
    session.add(
        _candidate(
            "darkflow-card:v2:strong-source",
            setup_time=now - timedelta(minutes=15),
            source_interaction_id=strong_source.id,
            strategy_id="pullback_to_cost",
            market_state="trend_pullback",
        )
    )
    session.add(
        _candidate(
            "darkflow-card:v2:weak-source",
            setup_time=now - timedelta(minutes=14),
            source_interaction_id=weak_source.id,
            strategy_id="liquidity_sweep_reversal",
            market_state="liquidity_hunt_reversal",
        )
    )
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=now, created_at=now))
    await session.commit()

    result = await accelerate_darkflow_alpha(
        session,
        candidate_limit=10,
        shadow_limit=5,
        materialize=False,
        mark_first=False,
        entry_tolerance_pct=0.01,
        paused_group_exploration_limit=1,
    )

    opened_keys = {item["candidate_key"] for item in result["steps"]["promotion_refresh"]["shadow_forward"]["opened"]}
    skipped = result["steps"]["promotion_refresh"]["shadow_forward"]["skipped"]
    shadow_rows = (await session.scalars(select(ShadowPaperTrade).where(ShadowPaperTrade.status == "open"))).all()
    paper_rows = (await session.scalars(select(PaperTrade))).all()

    assert "darkflow-card:v2:strong-source" in opened_keys
    assert "darkflow-card:v2:weak-source" not in opened_keys
    assert any(item["candidate_key"] == "darkflow-card:v2:weak-source" and item["reason"] == "darkflow_subportfolio_recommendation_paused" for item in skipped)
    assert result["sampling_plan"]["priority_group_count"] >= 1
    assert result["sampling_plan"]["paused_group_count"] >= 1
    assert result["sampling_plan"]["paused_subportfolio_count"] >= 1
    assert result["sampling_plan"]["paused_group_exploration_limit"] == 1
    assert all(row.context.get("opens_paper_trades") is False for row in shadow_rows)
    assert paper_rows == []


@pytest.mark.asyncio
async def test_accelerate_darkflow_alpha_can_disable_paused_group_exploration(session) -> None:
    now = datetime.now(timezone.utc)
    history_base = now - timedelta(hours=3)
    session.add_all(
        [
            _shadow_trade(
                f"weak-history-disabled-{index}",
                candidate_key=f"weak-disabled-candidate-{index}",
                symbol="BTCUSDT",
                direction="long",
                strategy_id="liquidity_sweep_reversal",
                market_state="liquidity_hunt_reversal",
                pnl=-0.02,
                opened_at=history_base + timedelta(minutes=index),
            )
            for index in range(3)
        ]
    )
    weak_source = _source_interaction(
        interaction_key="weak-source-disabled",
        playbook="liquidity_sweep_reversal",
        setup_time=now - timedelta(minutes=14),
    )
    session.add(weak_source)
    await session.flush()
    session.add(
        _candidate(
            "darkflow-card:v2:weak-source-disabled",
            setup_time=now - timedelta(minutes=14),
            source_interaction_id=weak_source.id,
            strategy_id="liquidity_sweep_reversal",
            market_state="liquidity_hunt_reversal",
        )
    )
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=now, created_at=now))
    await session.commit()

    result = await accelerate_darkflow_alpha(
        session,
        candidate_limit=10,
        shadow_limit=5,
        materialize=False,
        mark_first=False,
        entry_tolerance_pct=0.01,
        paused_group_exploration_limit=0,
    )

    opened_keys = {item["candidate_key"] for item in result["steps"]["promotion_refresh"]["shadow_forward"]["opened"]}
    skipped = result["steps"]["promotion_refresh"]["shadow_forward"]["skipped"]
    paper_rows = (await session.scalars(select(PaperTrade))).all()

    assert "darkflow-card:v2:weak-source-disabled" not in opened_keys
    assert any(item["candidate_key"] == "darkflow-card:v2:weak-source-disabled" and item["reason"] == "darkflow_subportfolio_recommendation_paused" for item in skipped)
    assert result["sampling_plan"]["paused_subportfolio_count"] >= 1
    assert result["sampling_plan"]["paused_group_exploration_limit"] == 0
    assert paper_rows == []


@pytest.mark.asyncio
async def test_accelerate_darkflow_alpha_blocks_recommendation_blacklist_even_with_exploration_budget(session) -> None:
    now = datetime.now(timezone.utc)
    history_base = now - timedelta(hours=3)
    session.add_all(
        [
            _shadow_trade(
                f"blacklist-history-{index}",
                candidate_key=f"blacklist-history-candidate-{index}",
                symbol="BTCUSDT",
                direction="long",
                strategy_id="trend_ride_extension",
                market_state="trend_extension",
                pnl=-0.02,
                opened_at=history_base + timedelta(minutes=index),
            )
            for index in range(6)
        ]
    )
    weak_source = _source_interaction(
        interaction_key="blacklist-source",
        playbook="trend_ride_extension",
        setup_time=now - timedelta(minutes=14),
    )
    session.add(weak_source)
    await session.flush()
    session.add(
        _candidate(
            "darkflow-card:v2:blacklist-source",
            setup_time=now - timedelta(minutes=14),
            source_interaction_id=weak_source.id,
            strategy_id="trend_ride_extension",
            market_state="trend_extension",
        )
    )
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=now, created_at=now))
    await session.commit()

    result = await accelerate_darkflow_alpha(
        session,
        candidate_limit=10,
        shadow_limit=5,
        materialize=False,
        mark_first=False,
        entry_tolerance_pct=0.01,
        paused_group_exploration_limit=3,
    )

    candidate = await session.scalar(select(TradeCandidate).where(TradeCandidate.candidate_key == "darkflow-card:v2:blacklist-source"))
    opened_keys = {item["candidate_key"] for item in result["steps"]["promotion_refresh"]["shadow_forward"]["opened"]}
    skipped = result["steps"]["promotion_refresh"]["shadow_forward"]["skipped"]

    assert "darkflow-card:v2:blacklist-source" not in opened_keys
    assert any(item["reason"] == "darkflow_subportfolio_recommendation_paused" for item in skipped)
    assert result["sampling_plan"]["paused_subportfolio_count"] >= 1
    assert candidate is not None
    assert candidate.status == "entry_plan_retired"
    assert candidate.decision_payload["darkflow_recommendation_gate"]["reason"] == "darkflow_subportfolio_recommendation_paused"


@pytest.mark.asyncio
async def test_deweighted_strategy_allows_whitelisted_subportfolio_only(session) -> None:
    now = datetime.now(timezone.utc)
    history_base = now - timedelta(hours=3)
    session.add_all(
        [
            _shadow_trade(
                f"whitelist-subportfolio-{index}",
                candidate_key=f"whitelist-subportfolio-candidate-{index}",
                symbol="HYPEUSDT",
                direction="long",
                strategy_id="trend_ride_extension",
                market_state="trend_extension",
                pnl=0.02,
                opened_at=history_base + timedelta(minutes=index),
            )
            for index in range(6)
        ]
        + [
            _shadow_trade(
                f"deweight-peer-{index}",
                candidate_key=f"deweight-peer-candidate-{index}",
                symbol="ETHUSDT",
                direction="short",
                strategy_id="trend_ride_extension",
                market_state="trend_extension",
                pnl=-0.02,
                opened_at=history_base + timedelta(hours=1, minutes=index),
            )
            for index in range(10)
        ]
    )
    whitelist_source = _source_interaction(
        interaction_key="whitelist-local-source",
        playbook="trend_ride_extension",
        symbol="HYPEUSDT",
        direction="long",
        setup_time=now - timedelta(minutes=15),
    )
    deweighted_source = _source_interaction(
        interaction_key="deweighted-local-source",
        playbook="trend_ride_extension",
        symbol="BTCUSDT",
        direction="long",
        setup_time=now - timedelta(minutes=16),
    )
    session.add_all([whitelist_source, deweighted_source])
    await session.flush()
    session.add(
        _candidate(
            "darkflow-card:v2:whitelist-local-source",
            setup_time=now - timedelta(minutes=15),
            source_interaction_id=whitelist_source.id,
            strategy_id="trend_ride_extension",
            market_state="trend_extension",
            symbol="HYPEUSDT",
        )
    )
    session.add(
        _candidate(
            "darkflow-card:v2:deweighted-local-source",
            setup_time=now - timedelta(minutes=16),
            source_interaction_id=deweighted_source.id,
            strategy_id="trend_ride_extension",
            market_state="trend_extension",
            symbol="BTCUSDT",
        )
    )
    session.add_all(
        [
            PriceSnapshot(symbol="HYPEUSDT", price=100.0, raw_payload={}, collected_at=now, created_at=now),
            PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=now, created_at=now),
        ]
    )
    await session.commit()

    result = await accelerate_darkflow_alpha(
        session,
        candidate_limit=20,
        shadow_limit=5,
        materialize=False,
        mark_first=False,
        entry_tolerance_pct=0.01,
        paused_group_exploration_limit=0,
    )

    opened_keys = {item["candidate_key"] for item in result["steps"]["promotion_refresh"]["shadow_forward"]["opened"]}
    skipped = result["steps"]["promotion_refresh"]["shadow_forward"]["skipped"]
    whitelist_trade = await session.scalar(select(ShadowPaperTrade).where(ShadowPaperTrade.candidate_key == "darkflow-card:v2:whitelist-local-source"))
    deweighted_candidate = await session.scalar(select(TradeCandidate).where(TradeCandidate.candidate_key == "darkflow-card:v2:deweighted-local-source"))

    assert "darkflow-card:v2:whitelist-local-source" in opened_keys
    assert "darkflow-card:v2:deweighted-local-source" not in opened_keys
    assert any(item["reason"] == "darkflow_strategy_deweighted_non_whitelist" for item in skipped)
    assert result["sampling_plan"]["deweighted_strategy_count"] >= 1
    assert whitelist_trade is not None
    assert whitelist_trade.context["darkflow_recommendation_gate"]["reason"] == "darkflow_subportfolio_whitelist"
    assert deweighted_candidate is not None
    assert deweighted_candidate.decision_payload["darkflow_recommendation_gate"]["reason"] == "darkflow_strategy_deweighted_non_whitelist"


@pytest.mark.asyncio
async def test_recommendation_pause_does_not_spend_legacy_exploration_budget(session) -> None:
    now = datetime.now(timezone.utc)
    history_base = now - timedelta(hours=3)
    session.add_all(
        [
            _shadow_trade(
                f"strong-budget-history-{index}",
                candidate_key=f"strong-budget-candidate-{index}",
                symbol="BTCUSDT",
                direction="long",
                strategy_id="pullback_to_cost",
                market_state="trend_pullback",
                pnl=0.02,
                opened_at=history_base + timedelta(minutes=index),
            )
            for index in range(3)
        ]
        + [
            _shadow_trade(
                f"weak-budget-history-{index}",
                candidate_key=f"weak-budget-candidate-{index}",
                symbol="BTCUSDT",
                direction="long",
                strategy_id="liquidity_sweep_reversal",
                market_state="liquidity_hunt_reversal",
                pnl=-0.02,
                opened_at=history_base + timedelta(minutes=30 + index),
            )
            for index in range(3)
        ]
    )
    expired_source = _source_interaction(
        interaction_key="weak-budget-expired",
        playbook="liquidity_sweep_reversal",
        setup_time=now - timedelta(minutes=10),
    )
    openable_source = _source_interaction(
        interaction_key="weak-budget-openable",
        playbook="liquidity_sweep_reversal",
        setup_time=now - timedelta(minutes=15),
    )
    session.add_all([expired_source, openable_source])
    await session.flush()
    expired_candidate = _candidate(
        "darkflow-card:v2:weak-budget-expired",
        setup_time=now - timedelta(minutes=10),
        source_interaction_id=expired_source.id,
        strategy_id="liquidity_sweep_reversal",
        market_state="liquidity_hunt_reversal",
    )
    expired_payload = dict(expired_candidate.decision_payload)
    expired_plan = dict(expired_payload["entry_plan"])
    expired_plan["valid_until"] = (now - timedelta(minutes=1)).isoformat()
    expired_payload["entry_plan"] = expired_plan
    expired_candidate.decision_payload = expired_payload
    session.add(expired_candidate)
    session.add(
        _candidate(
            "darkflow-card:v2:weak-budget-openable",
            setup_time=now - timedelta(minutes=15),
            source_interaction_id=openable_source.id,
            strategy_id="liquidity_sweep_reversal",
            market_state="liquidity_hunt_reversal",
        )
    )
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=now, created_at=now))
    await session.commit()

    result = await accelerate_darkflow_alpha(
        session,
        candidate_limit=10,
        shadow_limit=5,
        materialize=False,
        mark_first=False,
        entry_tolerance_pct=0.01,
        paused_group_exploration_limit=1,
    )

    opened_keys = {item["candidate_key"] for item in result["steps"]["promotion_refresh"]["shadow_forward"]["opened"]}
    skipped = result["steps"]["promotion_refresh"]["shadow_forward"]["skipped"]
    shadow_rows = (await session.scalars(select(ShadowPaperTrade).where(ShadowPaperTrade.status == "open"))).all()

    assert "darkflow-card:v2:weak-budget-expired" not in opened_keys
    assert "darkflow-card:v2:weak-budget-openable" not in opened_keys
    assert any(item["candidate_key"] == "darkflow-card:v2:weak-budget-expired" and item["reason"] == "entry_plan_expired" for item in skipped)
    assert any(item["candidate_key"] == "darkflow-card:v2:weak-budget-openable" and item["reason"] == "darkflow_subportfolio_recommendation_paused" for item in skipped)
    assert shadow_rows == []


@pytest.mark.asyncio
async def test_accelerate_darkflow_alpha_retires_expired_materialized_candidates_before_shadow_scan(session) -> None:
    now = datetime.now(timezone.utc)
    expired_source = _source_interaction(interaction_key="alpha-expired-source", setup_time=now - timedelta(hours=8))
    fresh_source = _source_interaction(interaction_key="alpha-fresh-source", setup_time=now - timedelta(minutes=15))
    session.add_all([expired_source, fresh_source])
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=now, created_at=now))
    await session.commit()

    result = await accelerate_darkflow_alpha(
        session,
        candidate_limit=10,
        shadow_limit=5,
        materialize=True,
        mark_first=False,
        entry_tolerance_pct=0.01,
    )

    expired = await session.scalar(select(TradeCandidate).where(TradeCandidate.candidate_key == "darkflow-card:v2:alpha-expired-source"))
    fresh = await session.scalar(select(TradeCandidate).where(TradeCandidate.candidate_key == "darkflow-card:v2:alpha-fresh-source"))
    shadow_rows = (await session.scalars(select(ShadowPaperTrade).where(ShadowPaperTrade.status == "open"))).all()
    materialize = result["steps"]["promotion_refresh"]["materialize"]
    shadow = result["steps"]["promotion_refresh"]["shadow_forward"]

    assert materialize["expired_entry_plan_retired_count"] == 1
    assert expired is not None
    assert expired.status == "entry_plan_retired"
    assert expired.shadow_status == "retired"
    assert expired.decision_payload["entry_plan_retirement"]["reason"] == "entry_plan_expired"
    assert fresh is not None
    assert fresh.status == "shadow_candidate"
    assert shadow["opened_count"] == 1
    assert shadow_rows[0].candidate_key == "darkflow-card:v2:alpha-fresh-source"
    assert not any(item["candidate_key"] == "darkflow-card:v2:alpha-expired-source" for item in shadow["skipped"])


@pytest.mark.asyncio
async def test_accelerate_darkflow_alpha_updates_existing_expired_candidate_lifecycle_consistently(session) -> None:
    now = datetime.now(timezone.utc)
    expired_source = _source_interaction(interaction_key="alpha-existing-expired", setup_time=now - timedelta(hours=8))
    session.add(expired_source)
    await session.flush()
    stale_candidate = _candidate(
        "darkflow-card:v2:alpha-existing-expired",
        setup_time=now - timedelta(hours=8),
        source_interaction_id=expired_source.id,
    )
    stale_candidate.status = "research_blocked"
    stale_candidate.promotion_status = "blocked"
    stale_candidate.anti_repaint_status = "missing"
    stale_candidate.shadow_status = "not_started"
    session.add(stale_candidate)
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=now, created_at=now))
    await session.commit()

    result = await accelerate_darkflow_alpha(
        session,
        candidate_limit=10,
        shadow_limit=5,
        materialize=True,
        mark_first=False,
        entry_tolerance_pct=0.01,
    )

    refreshed = await session.scalar(select(TradeCandidate).where(TradeCandidate.candidate_key == "darkflow-card:v2:alpha-existing-expired"))

    assert result["steps"]["promotion_refresh"]["materialize"]["expired_entry_plan_retired_count"] == 1
    assert refreshed is not None
    assert refreshed.status == "entry_plan_retired"
    assert refreshed.promotion_status == "entry_plan_retired"
    assert refreshed.anti_repaint_status == "passed"
    assert refreshed.shadow_status == "retired"
    assert refreshed.promotion_blockers == ["entry_plan_retired"]


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
    exit_reason: str | None = None,
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
        exit_reason=exit_reason or ("take_profit" if pnl > 0 else "stop_loss"),
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


def _candidate(
    candidate_key: str,
    *,
    setup_time: datetime,
    source_interaction_id: str | None = None,
    strategy_id: str = "pullback_to_cost",
    market_state: str = "trend_pullback",
    symbol: str = "BTCUSDT",
    direction: str = "long",
) -> TradeCandidate:
    return TradeCandidate(
        candidate_key=candidate_key,
        source_type="darkflow_interaction",
        source_interaction_id=source_interaction_id,
        lineage="core_darkflow_v2",
        strategy_family="darkflow_v2",
        strategy_id=strategy_id,
        strategy_name=strategy_id.replace("_", " ").title(),
        symbol=symbol,
        timeframe="short",
        interval="30m",
        direction=direction,
        setup_type="first_touch",
        market_state=market_state,
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


def _source_interaction(
    *,
    setup_time: datetime,
    interaction_key: str = "source-triggered",
    playbook: str = "pullback_to_cost",
    symbol: str = "BTCUSDT",
    direction: str = "long",
) -> DarkflowInteraction:
    return DarkflowInteraction(
        interaction_key=interaction_key,
        zone_key=f"zone-{interaction_key}",
        source_snapshot_id=f"snapshot-{interaction_key}",
        symbol=symbol,
        timeframe="short",
        interval="30m",
        indicator="trend_price",
        playbook=playbook,
        direction=direction,
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
