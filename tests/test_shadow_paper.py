from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import ExperimentRun, FeatureEvent, FeatureLabel, PaperTrade, PriceSnapshot, ShadowPaperTrade
from app.services.shadow_paper import (
    SHADOW_FEE_RATE,
    darkflow_playbook_attribution_report,
    darkflow_subportfolio_recommendations_report,
    darkflow_trend_extension_exit_report,
    mark_shadow_paper_trades,
    shadow_paper_replay,
    shadow_paper_replay_all,
    shadow_paper_scan,
    shadow_paper_stats,
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
async def test_shadow_paper_scan_uses_materialized_watchlist_without_real_trade(session) -> None:
    session.add(
        ExperimentRun(
            name="feature_segment_candidates_30m",
            status="research",
            scope={},
            params={},
            metrics={
                "candidates": [],
                "all_segments": [
                    {
                        "segment_key": "inst_vwap:unknown:long:BTCUSDT:short",
                        "feature_key": "inst_vwap:unknown:long",
                        "symbol": "BTCUSDT",
                        "timeframe": "short",
                        "direction": "long",
                        "sample_count": 30,
                        "win_rate": 0.6,
                        "avg_return": 0.01,
                        "profit_factor": 1.5,
                        "promotion_status": "watchlist",
                    }
                ],
            },
            notes="test",
        )
    )
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    await session.commit()

    result = await shadow_paper_scan(session, candidate_limit=5)
    rows = await session.execute(select(ShadowPaperTrade))
    trades = rows.scalars().all()

    assert result["policy"]["opens_paper_trades"] is False
    assert len(result["opened"]) == 1
    assert len(trades) == 1
    assert trades[0].candidate_type == "observation_segment"
    assert result["opened"][0]["id"] == trades[0].id
    assert trades[0].status == "open"
    assert trades[0].entry_price > 100.0
    assert trades[0].context["execution_model"]["entry_and_exit_use_worse_price"] is True


@pytest.mark.asyncio
async def test_shadow_paper_replay_creates_closed_historical_trades_without_real_paper(session) -> None:
    await add_replay_segment_report(session)
    await add_labeled_replay_features(session, returns=[0.02, 0.015, -0.004])
    await session.commit()

    result = await shadow_paper_replay(session, horizon="30m", limit=10, candidate_limit=5)
    replay_again = await shadow_paper_replay(session, horizon="30m", limit=10, candidate_limit=5)
    shadow_rows = await session.execute(select(ShadowPaperTrade).order_by(ShadowPaperTrade.opened_at))
    paper_rows = await session.execute(select(PaperTrade))
    trades = shadow_rows.scalars().all()

    assert result["policy"]["opens_live_orders"] is False
    assert result["policy"]["opens_paper_trades"] is False
    assert result["inserted"] == 3
    assert replay_again["inserted"] == 0
    assert replay_again["duplicates"] == 3
    assert len(trades) == 3
    assert {trade.status for trade in trades} == {"closed"}
    assert {trade.context["historical_replay"] for trade in trades} == {True}
    assert max(float(trade.pnl or 0.0) for trade in trades) == pytest.approx(0.02 - SHADOW_FEE_RATE * 2, rel=0.2)
    assert paper_rows.scalars().all() == []


@pytest.mark.asyncio
async def test_shadow_paper_replay_all_keeps_horizon_evidence_separate(session) -> None:
    await add_replay_segment_report(session, horizons=["30m", "1h"])
    await add_labeled_replay_features(session, returns=[0.02, -0.004], horizons=["30m", "1h"])
    await session.commit()

    result = await shadow_paper_replay_all(session, horizons=["30m", "1h"], limit=10, candidate_limit=5)
    stats = await shadow_paper_stats(session)
    paper_rows = await session.execute(select(PaperTrade))

    assert result["policy"]["opens_paper_trades"] is False
    assert result["policy"]["opens_live_orders"] is False
    assert result["inserted"] == 4
    assert result["results"]["30m"]["inserted"] == 2
    assert result["results"]["1h"]["inserted"] == 2
    assert {row["horizon"] for row in stats["by_horizon"]} == {"30m", "1h"}
    assert {row["horizon"] for row in stats["by_candidate"]} == {"30m", "1h"}
    assert paper_rows.scalars().all() == []


@pytest.mark.asyncio
async def test_mark_shadow_paper_trades_closes_take_profit(session) -> None:
    session.add(
        ShadowPaperTrade(
            strategy_name="shadow_feature_candidates_v1",
            candidate_type="watchlist_segment",
            candidate_key="candidate-1",
            signal_key="signal-1",
            symbol="BTCUSDT",
            timeframe="short",
            direction="long",
            entry_price=100.0,
            stop_loss=99.0,
            take_profit=102.0,
            position_size=1.0,
            status="open",
            context={},
        )
    )
    session.add(PriceSnapshot(symbol="BTCUSDT", price=103.0, raw_payload={}, collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    await session.commit()

    mark = await mark_shadow_paper_trades(session)
    stats = await shadow_paper_stats(session)
    stored = await session.scalar(select(ShadowPaperTrade))

    assert mark["closed"][0]["exit_reason"] == "take_profit"
    assert stored.status == "closed"
    assert stored.pnl == pytest.approx(((103.0 * (1 - 0.0002)) - 100.0) / 100.0 - SHADOW_FEE_RATE * 2)
    assert stored.context["exit_execution_price"] < 103.0
    assert stats["closed_trades"] == 1
    assert stats["policy"]["opens_live_orders"] is False
    assert stats["policy"]["lineage"]["lineage"] == "legacy_feature_research"
    assert stats["policy"]["lineage"]["legacy_control"] is True
    assert stats["policy"]["uses_fee_and_slippage"] is True


@pytest.mark.asyncio
async def test_mark_shadow_paper_trades_time_closes_expired_darkflow_shadow_forward(session) -> None:
    now = datetime.now(timezone.utc)
    session.add(
        ShadowPaperTrade(
            strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
            candidate_type="trade_candidate",
            candidate_key="darkflow-expired-candidate",
            signal_key="darkflow-expired-signal",
            symbol="BTCUSDT",
            timeframe="short",
            direction="long",
            entry_price=100.0,
            stop_loss=98.0,
            take_profit=105.0,
            position_size=1.0,
            status="open",
            opened_at=now - timedelta(hours=3),
            context={
                "shadow_forward": True,
                "entry_plan_state": {
                    "state": "triggered",
                    "valid_until": (now - timedelta(minutes=30)).isoformat(),
                },
                "candidate_snapshot": {"interval": "30m"},
            },
        )
    )
    session.add(PriceSnapshot(symbol="BTCUSDT", price=101.0, raw_payload={}, collected_at=now))
    await session.commit()

    mark = await mark_shadow_paper_trades(session)
    stored = await session.scalar(select(ShadowPaperTrade))

    assert mark["closed"][0]["exit_reason"] == "shadow_forward_time_exit"
    assert stored.status == "closed"
    assert stored.exit_reason == "shadow_forward_time_exit"
    assert stored.context["closed_by_shadow_mark"] is True
    assert stored.context["shadow_forward_time_exit"] is True
    assert stored.context["time_exit_basis"] == "entry_plan_valid_until"
    assert stored.context["max_hold_until"] == (now - timedelta(minutes=30)).isoformat()
    assert mark["policy"]["opens_paper_trades"] is False
    assert mark["policy"]["opens_live_orders"] is False


@pytest.mark.asyncio
async def test_mark_shadow_paper_trades_time_closes_expired_darkflow_shadow_forward_without_latest_price(session) -> None:
    now = datetime.now(timezone.utc)
    session.add(
        ShadowPaperTrade(
            strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
            candidate_type="trade_candidate",
            candidate_key="darkflow-expired-no-price-candidate",
            signal_key="darkflow-expired-no-price-signal",
            symbol="NO_PRICE_USDT",
            timeframe="short",
            direction="long",
            entry_price=100.0,
            stop_loss=98.0,
            take_profit=105.0,
            position_size=1.0,
            status="open",
            opened_at=now - timedelta(hours=3),
            context={
                "shadow_forward": True,
                "last_mark_price": 101.0,
                "entry_plan_state": {
                    "state": "triggered",
                    "valid_until": (now - timedelta(minutes=30)).isoformat(),
                },
                "candidate_snapshot": {"interval": "30m"},
            },
        )
    )
    await session.commit()

    mark = await mark_shadow_paper_trades(session)
    stored = await session.scalar(select(ShadowPaperTrade))

    assert mark["closed"][0]["exit_reason"] == "shadow_forward_time_exit"
    assert stored.status == "closed"
    assert stored.exit_reason == "shadow_forward_time_exit"
    assert stored.context["shadow_forward_time_exit"] is True
    assert stored.context["missing_latest_price_at_time_exit"] is True
    assert stored.context["fallback_mark_price_source"] == "last_mark_price"
    assert stored.context["exit_mark_price"] == 101.0
    assert stored.pnl == pytest.approx(((101.0 * (1 - 0.0007)) - 100.0) / 100.0 - SHADOW_FEE_RATE * 2)


@pytest.mark.asyncio
async def test_mark_shadow_paper_trades_keeps_fresh_darkflow_shadow_forward_open(session) -> None:
    now = datetime.now(timezone.utc)
    session.add(
        ShadowPaperTrade(
            strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
            candidate_type="trade_candidate",
            candidate_key="darkflow-fresh-candidate",
            signal_key="darkflow-fresh-signal",
            symbol="BTCUSDT",
            timeframe="short",
            direction="long",
            entry_price=100.0,
            stop_loss=98.0,
            take_profit=105.0,
            position_size=1.0,
            status="open",
            opened_at=now - timedelta(minutes=20),
            context={
                "shadow_forward": True,
                "entry_plan_state": {
                    "state": "triggered",
                    "valid_until": (now + timedelta(hours=1)).isoformat(),
                },
                "candidate_snapshot": {"interval": "30m"},
            },
        )
    )
    session.add(PriceSnapshot(symbol="BTCUSDT", price=101.0, raw_payload={}, collected_at=now))
    await session.commit()

    mark = await mark_shadow_paper_trades(session)
    stored = await session.scalar(select(ShadowPaperTrade))

    assert mark["closed"] == []
    assert mark["updated"][0]["symbol"] == "BTCUSDT"
    assert stored.status == "open"
    assert stored.exit_reason is None
    assert stored.context["last_mark_price"] == 101.0
    assert "shadow_forward_time_exit" not in stored.context


@pytest.mark.asyncio
async def test_mark_shadow_paper_trades_does_not_time_close_legacy_shadow_trades(session) -> None:
    now = datetime.now(timezone.utc)
    session.add(
        ShadowPaperTrade(
            strategy_name="shadow_feature_candidates_v1",
            candidate_type="observation_segment",
            candidate_key="legacy-candidate",
            signal_key="legacy-signal-no-time-exit",
            symbol="BTCUSDT",
            timeframe="short",
            direction="long",
            entry_price=100.0,
            stop_loss=98.0,
            take_profit=105.0,
            position_size=1.0,
            status="open",
            opened_at=now - timedelta(hours=24),
            context={
                "entry_plan_state": {
                    "state": "triggered",
                    "valid_until": (now - timedelta(hours=12)).isoformat(),
                },
                "candidate_snapshot": {"interval": "30m"},
            },
        )
    )
    session.add(PriceSnapshot(symbol="BTCUSDT", price=101.0, raw_payload={}, collected_at=now))
    await session.commit()

    mark = await mark_shadow_paper_trades(session)
    stored = await session.scalar(select(ShadowPaperTrade))

    assert mark["closed"] == []
    assert stored.status == "open"
    assert stored.exit_reason is None
    assert stored.context["last_mark_price"] == 101.0
    assert "shadow_forward_time_exit" not in stored.context


@pytest.mark.asyncio
async def test_mark_shadow_paper_trades_extends_high_volatility_runner(session) -> None:
    session.add(
        ShadowPaperTrade(
            strategy_name="shadow_feature_candidates_v1",
            candidate_type="observation_segment",
            candidate_key="liquidity_sweep:bullish_sweep:long:HYPEUSDT:long",
            signal_key="signal-hype-runner",
            symbol="HYPEUSDT",
            timeframe="long",
            direction="long",
            entry_price=38.0,
            stop_loss=37.05,
            take_profit=39.52,
            position_size=1.0,
            status="open",
            context={
                "candidate_snapshot": {
                    "segment_key": "liquidity_sweep:bullish_sweep:long:HYPEUSDT:long",
                    "feature_key": "trend_price.order_blocks:Accumulation:long",
                    "sample_count": 12,
                    "raw_sample_count": 80,
                    "win_rate": 0.58,
                    "avg_return": 0.014,
                    "profit_factor": 1.4,
                    "reliability_score": 0.22,
                    "promotion_status": "watchlist",
                },
                "execution_model": {"mode": "conservative_shadow_paper"},
            },
        )
    )
    session.add(PriceSnapshot(symbol="HYPEUSDT", price=39.6, raw_payload={}, collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    await session.commit()

    mark = await mark_shadow_paper_trades(session)
    stored = await session.scalar(select(ShadowPaperTrade))

    assert mark["closed"] == []
    assert mark["updated"][0]["runner_extended"] is True
    assert mark["updated"][0]["runner_evidence"]["extend"] is True
    assert stored.status == "open"
    assert stored.stop_loss > 38.0
    assert stored.take_profit > 39.6
    assert stored.context["runner_decision"]["signals"]["trend_aligned"] is True


@pytest.mark.asyncio
async def test_mark_shadow_paper_trades_closes_when_runner_candidate_is_weak(session) -> None:
    session.add(
        ShadowPaperTrade(
            strategy_name="shadow_feature_candidates_v1",
            candidate_type="observation_segment",
            candidate_key="weak:unknown:long:HYPEUSDT:long",
            signal_key="signal-hype-weak",
            symbol="HYPEUSDT",
            timeframe="long",
            direction="long",
            entry_price=38.0,
            stop_loss=37.05,
            take_profit=39.52,
            position_size=1.0,
            status="open",
            context={"candidate_snapshot": {"sample_count": 1, "win_rate": 0.3, "profit_factor": 0.7}},
        )
    )
    session.add(PriceSnapshot(symbol="HYPEUSDT", price=39.6, raw_payload={}, collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    await session.commit()

    mark = await mark_shadow_paper_trades(session)
    stored = await session.scalar(select(ShadowPaperTrade))

    assert mark["closed"][0]["exit_reason"] == "take_profit"
    assert stored.status == "closed"
    assert stored.context["runner_decision"]["extend"] is False


@pytest.mark.asyncio
async def test_shadow_paper_stats_groups_by_candidate(session) -> None:
    opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add_all(
        [
            ShadowPaperTrade(
                strategy_name="shadow_feature_candidates_v1",
                candidate_type="segment_candidate",
                candidate_key="candidate-a",
                signal_key="signal-a1",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=102.0,
                position_size=1.0,
                status="closed",
                exit_price=102.0,
                exit_reason="take_profit",
                pnl=0.02,
                opened_at=opened_at,
                closed_at=opened_at,
                context={"horizon": "30m"},
            ),
            ShadowPaperTrade(
                strategy_name="shadow_feature_candidates_v1",
                candidate_type="segment_candidate",
                candidate_key="candidate-a",
                signal_key="signal-a2",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=102.0,
                position_size=1.0,
                status="closed",
                exit_price=99.0,
                exit_reason="stop_loss",
                pnl=-0.01,
                opened_at=opened_at,
                closed_at=opened_at,
                context={"horizon": "30m"},
            ),
            ShadowPaperTrade(
                strategy_name="shadow_feature_candidates_v1",
                candidate_type="observation_segment",
                candidate_key="candidate-b",
                signal_key="signal-b1",
                symbol="ETHUSDT",
                timeframe="short",
                direction="short",
                entry_price=100.0,
                stop_loss=101.0,
                take_profit=98.0,
                position_size=1.0,
                status="open",
                opened_at=opened_at,
                context={"horizon": "1h"},
            ),
        ]
    )
    await session.commit()

    stats = await shadow_paper_stats(session)
    candidate_a = next(item for item in stats["by_candidate"] if item["candidate_key"] == "candidate-a")

    assert stats["total_trades"] == 3
    assert stats["closed_trades"] == 2
    assert candidate_a["total_trades"] == 2
    assert candidate_a["closed_trades"] == 2
    assert candidate_a["horizon"] == "30m"
    assert {row["horizon"] for row in stats["by_horizon"]} == {"30m", "1h"}
    assert candidate_a["win_rate"] == 0.5
    assert candidate_a["profit_factor"] == 2.0
    assert candidate_a["max_drawdown"] > 0
    assert stats["by_symbol"][0]["symbol"] in {"BTCUSDT", "ETHUSDT"}


@pytest.mark.asyncio
async def test_shadow_paper_stats_reports_unique_plan_deduped_metrics(session) -> None:
    opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    common_context = {
        "horizon": "live",
        "shadow_plan_fingerprint": "pullback:BTCUSDT:short:long:100.0:99.0:102.0",
    }
    session.add_all(
        [
            ShadowPaperTrade(
                strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
                candidate_type="trade_candidate",
                candidate_key="candidate-a",
                signal_key="signal-duplicate-win",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=102.0,
                position_size=1.0,
                status="closed",
                pnl=0.02,
                opened_at=opened_at,
                closed_at=opened_at + timedelta(minutes=30),
                context=common_context,
            ),
            ShadowPaperTrade(
                strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
                candidate_type="trade_candidate",
                candidate_key="candidate-b",
                signal_key="signal-duplicate-loss",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=102.0,
                position_size=1.0,
                status="closed",
                pnl=-0.01,
                opened_at=opened_at,
                closed_at=opened_at + timedelta(minutes=10),
                context=common_context,
            ),
            ShadowPaperTrade(
                strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
                candidate_type="trade_candidate",
                candidate_key="candidate-c",
                signal_key="signal-unique-loss",
                symbol="ETHUSDT",
                timeframe="short",
                direction="short",
                entry_price=100.0,
                stop_loss=101.0,
                take_profit=98.0,
                position_size=1.0,
                status="closed",
                pnl=-0.01,
                opened_at=opened_at,
                closed_at=opened_at + timedelta(minutes=20),
                context={"horizon": "live", "shadow_plan_fingerprint": "pullback:ETHUSDT:short:short:100.0:101.0:98.0"},
            ),
        ]
    )
    await session.commit()

    stats = await shadow_paper_stats(session, strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1")
    unique = stats["unique_plan_stats"]

    assert stats["closed_trades"] == 3
    assert stats["profit_factor"] == pytest.approx(1.0)
    assert unique["source_trade_count"] == 3
    assert unique["duplicate_trade_count"] == 1
    assert unique["closed_trades"] == 2
    assert unique["win_rate"] == 0.5
    assert unique["profit_factor"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_shadow_paper_stats_can_filter_by_strategy(session) -> None:
    opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add_all(
        [
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
                take_profit=102.0,
                position_size=1.0,
                status="closed",
                pnl=-0.01,
                opened_at=opened_at,
                closed_at=opened_at,
                context={"horizon": "30m"},
            ),
            ShadowPaperTrade(
                strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
                candidate_type="trade_candidate",
                candidate_key="darkflow-candidate",
                signal_key="darkflow-signal",
                symbol="HYPEUSDT",
                timeframe="short",
                direction="long",
                entry_price=38.0,
                stop_loss=37.0,
                take_profit=40.0,
                position_size=1.0,
                status="closed",
                pnl=0.03,
                opened_at=opened_at,
                closed_at=opened_at,
                context={"horizon": "4h"},
            ),
        ]
    )
    await session.commit()

    all_stats = await shadow_paper_stats(session)
    darkflow_stats = await shadow_paper_stats(session, strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1")

    assert all_stats["strategy_name"] == "all_shadow_strategies"
    assert all_stats["total_trades"] == 2
    assert darkflow_stats["strategy_name"] == "darkflow_v2_trade_candidate_shadow_forward_v1"
    assert darkflow_stats["total_trades"] == 1
    assert darkflow_stats["closed_trades"] == 1
    assert darkflow_stats["win_rate"] == 1.0
    assert darkflow_stats["by_candidate"][0]["candidate_key"] == "darkflow-candidate"


@pytest.mark.asyncio
async def test_shadow_paper_stats_orders_drawdown_by_close_time(session) -> None:
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add_all(
        [
            ShadowPaperTrade(
                strategy_name="shadow_feature_candidates_v1",
                candidate_type="segment_candidate",
                candidate_key="candidate-order",
                signal_key="signal-order-win",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=102.0,
                position_size=1.0,
                status="closed",
                pnl=0.5,
                opened_at=base_ts,
                closed_at=base_ts + timedelta(hours=2),
                context={"horizon": "4h"},
            ),
            ShadowPaperTrade(
                strategy_name="shadow_feature_candidates_v1",
                candidate_type="segment_candidate",
                candidate_key="candidate-order",
                signal_key="signal-order-loss",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=102.0,
                position_size=1.0,
                status="closed",
                pnl=-0.5,
                opened_at=base_ts,
                closed_at=base_ts + timedelta(hours=1),
                context={"horizon": "4h"},
            ),
        ]
    )
    await session.commit()

    stats = await shadow_paper_stats(session)

    assert stats["max_drawdown"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_shadow_paper_promotion_marks_ready_candidates(session) -> None:
    opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(30):
        pnl = 0.02 if index < 20 else -0.01
        session.add(
            ShadowPaperTrade(
                strategy_name="shadow_feature_candidates_v1",
                candidate_type="segment_candidate",
                candidate_key="candidate-ready",
                signal_key=f"signal-ready-{index}",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=102.0,
                position_size=1.0,
                status="closed",
                exit_price=102.0 if pnl > 0 else 99.0,
                exit_reason="take_profit" if pnl > 0 else "stop_loss",
                pnl=pnl,
                opened_at=opened_at,
                closed_at=opened_at,
                context={"execution_model": {"mode": "conservative_shadow_paper"}},
            )
        )
    await session.commit()

    stats = await shadow_paper_stats(session)

    ready = stats["promotion"]["ready"]
    assert len(ready) == 1
    assert ready[0]["candidate_key"] == "candidate-ready"
    assert ready[0]["promotion_blockers"] == []


@pytest.mark.asyncio
async def test_shadow_paper_promotion_separates_positive_edge_from_unstable_drawdown(session) -> None:
    opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(30):
        pnl = 0.04 if index < 20 else -0.02
        session.add(
            ShadowPaperTrade(
                strategy_name="shadow_feature_candidates_v1",
                candidate_type="segment_candidate",
                candidate_key="candidate-edge-unstable",
                signal_key=f"signal-edge-{index}",
                symbol="SOLUSDT",
                timeframe="short",
                direction="short",
                entry_price=100.0,
                stop_loss=101.0,
                take_profit=98.0,
                position_size=1.0,
                status="closed",
                pnl=pnl,
                opened_at=opened_at + timedelta(minutes=index),
                closed_at=opened_at + timedelta(minutes=index),
                context={"execution_model": {"mode": "conservative_shadow_paper"}, "horizon": "4h"},
            )
        )
    await session.commit()

    stats = await shadow_paper_stats(session)

    unstable = stats["promotion"]["edge_unstable"]
    assert len(unstable) == 1
    assert unstable[0]["candidate_key"] == "candidate-edge-unstable"
    assert unstable[0]["promotion_status"] == "edge_unstable_drawdown"
    assert unstable[0]["avg_pnl"] > 0
    assert unstable[0]["profit_factor"] >= 1.25
    assert unstable[0]["promotion_blockers"] == ["drawdown_above_threshold"]
    assert stats["promotion"]["ready"] == []


async def add_replay_segment_report(session, *, horizons: list[str] | None = None) -> None:
    for horizon in horizons or ["30m"]:
        session.add(
            ExperimentRun(
                name=f"feature_segment_candidates_{horizon}",
                status="research",
                scope={},
                params={},
                metrics={
                    "horizon": horizon,
                    "candidates": [],
                    "all_segments": [
                        {
                            "segment_key": "liquidity_sweep:bullish_sweep:long:HYPEUSDT:long",
                            "feature_key": "liquidity_sweep:bullish_sweep:long",
                            "feature_name": "liquidity_sweep",
                            "subtype": "bullish_sweep",
                            "symbol": "HYPEUSDT",
                            "timeframe": "long",
                            "direction": "long",
                            "sample_count": 30,
                            "raw_sample_count": 80,
                            "win_rate": 0.6,
                            "avg_return": 0.012,
                            "profit_factor": 1.6,
                            "promotion_status": "watchlist",
                        }
                    ],
                },
                notes="test",
            )
        )


async def add_labeled_replay_features(session, *, returns: list[float], horizons: list[str] | None = None) -> None:
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, return_pct in enumerate(returns):
        event_ts = base_ts + timedelta(minutes=index * 31)
        event = FeatureEvent(
            snapshot_id=f"snapshot-replay-{index}",
            symbol="HYPEUSDT",
            asset_tier="high_volatility",
            timeframe="long",
            interval="30m",
            indicator="liquidity_sweep",
            event_key=f"event-replay-{index}",
            feature_name="liquidity_sweep",
            direction="long",
            event_ts=event_ts,
            event_price=100.0,
            strength=0.8,
            subtype="bullish_sweep",
            source_payload_key="liquidity_sweep",
            context={},
        )
        session.add(event)
        await session.flush()
        for horizon in horizons or ["30m"]:
            session.add(
                FeatureLabel(
                    feature_event_id=event.id,
                    horizon=horizon,
                    return_pct=return_pct,
                    mfe=max(return_pct, 0.0) + 0.002,
                    mae=min(return_pct, 0.0) - 0.001,
                    future_price=100.0 * (1 + return_pct),
                    future_at=event_ts + {"30m": timedelta(minutes=30), "1h": timedelta(hours=1), "4h": timedelta(hours=4), "24h": timedelta(hours=24)}[horizon],
                    status="labeled",
                )
            )


@pytest.mark.asyncio
async def test_darkflow_playbook_attribution_report_groups_exit_mix_by_playbook(session) -> None:
    opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add_all(
        [
            ShadowPaperTrade(
                strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
                candidate_type="trade_candidate",
                candidate_key="candidate-pullback-a",
                signal_key="signal-pullback-a",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=103.0,
                position_size=1.0,
                status="closed",
                pnl=0.02,
                exit_reason="take_profit",
                opened_at=opened_at,
                closed_at=opened_at + timedelta(minutes=30),
                context={"candidate_snapshot": {"strategy_id": "pullback_to_cost", "market_state": "trend_pullback", "strategy_name": "成本带回踩"}, "shadow_plan_fingerprint": "pullback-a"},
            ),
            ShadowPaperTrade(
                strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
                candidate_type="trade_candidate",
                candidate_key="candidate-pullback-b",
                signal_key="signal-pullback-b",
                symbol="BTCUSDT",
                timeframe="short",
                direction="long",
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=103.0,
                position_size=1.0,
                status="closed",
                pnl=-0.01,
                exit_reason="stop_loss",
                opened_at=opened_at,
                closed_at=opened_at + timedelta(minutes=20),
                context={"candidate_snapshot": {"strategy_id": "pullback_to_cost", "market_state": "trend_pullback", "strategy_name": "成本带回踩"}, "shadow_plan_fingerprint": "pullback-b"},
            ),
            ShadowPaperTrade(
                strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
                candidate_type="trade_candidate",
                candidate_key="candidate-trend-a",
                signal_key="signal-trend-a",
                symbol="HYPEUSDT",
                timeframe="short",
                direction="long",
                entry_price=38.0,
                stop_loss=37.0,
                take_profit=40.0,
                position_size=1.0,
                status="closed",
                pnl=0.03,
                exit_reason="take_profit",
                opened_at=opened_at,
                closed_at=opened_at + timedelta(minutes=90),
                context={"candidate_snapshot": {"strategy_id": "trend_ride_extension", "market_state": "trend_extension", "strategy_name": "趋势延展"}, "shadow_plan_fingerprint": "trend-a"},
            ),
            ShadowPaperTrade(
                strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
                candidate_type="trade_candidate",
                candidate_key="candidate-trend-b",
                signal_key="signal-trend-b",
                symbol="HYPEUSDT",
                timeframe="short",
                direction="long",
                entry_price=38.0,
                stop_loss=37.0,
                take_profit=40.0,
                position_size=1.0,
                status="closed",
                pnl=0.01,
                exit_reason="shadow_forward_time_exit",
                opened_at=opened_at,
                closed_at=opened_at + timedelta(minutes=120),
                context={"candidate_snapshot": {"strategy_id": "trend_ride_extension", "market_state": "trend_extension", "strategy_name": "趋势延展"}, "shadow_plan_fingerprint": "trend-b"},
            ),
        ]
    )
    await session.commit()

    report = await darkflow_playbook_attribution_report(session)

    pullback = next(item for item in report["rows"] if item["strategy_id"] == "pullback_to_cost")
    trend = next(item for item in report["rows"] if item["strategy_id"] == "trend_ride_extension")

    assert report["strategy_name"] == "darkflow_v2_trade_candidate_shadow_forward_v1"
    assert pullback["closed_trades"] == 2
    assert pullback["exit_reason_counts"]["take_profit"] == 1
    assert pullback["exit_reason_counts"]["stop_loss"] == 1
    assert trend["closed_trades"] == 2
    assert trend["exit_reason_counts"]["take_profit"] == 1
    assert trend["exit_reason_counts"]["shadow_forward_time_exit"] == 1


@pytest.mark.asyncio
async def test_darkflow_trend_extension_exit_report_summarizes_runner_vs_time_exit(session) -> None:
    opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add_all(
        [
            ShadowPaperTrade(
                strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
                candidate_type="trade_candidate",
                candidate_key="trend-a",
                signal_key="trend-a",
                symbol="HYPEUSDT",
                timeframe="short",
                direction="long",
                entry_price=38.0,
                stop_loss=37.0,
                take_profit=40.0,
                position_size=1.0,
                status="closed",
                pnl=0.03,
                exit_reason="take_profit",
                opened_at=opened_at,
                closed_at=opened_at + timedelta(minutes=120),
                context={"candidate_snapshot": {"strategy_id": "trend_ride_extension", "market_state": "trend_extension"}, "shadow_plan_fingerprint": "trend-a"},
            ),
            ShadowPaperTrade(
                strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
                candidate_type="trade_candidate",
                candidate_key="trend-b",
                signal_key="trend-b",
                symbol="TONUSDT",
                timeframe="short",
                direction="long",
                entry_price=1.9,
                stop_loss=1.85,
                take_profit=2.0,
                position_size=1.0,
                status="closed",
                pnl=0.01,
                exit_reason="shadow_forward_time_exit",
                opened_at=opened_at,
                closed_at=opened_at + timedelta(minutes=180),
                context={"candidate_snapshot": {"strategy_id": "trend_ride_extension", "market_state": "trend_extension"}, "shadow_plan_fingerprint": "trend-b"},
            ),
        ]
    )
    await session.commit()

    report = await darkflow_trend_extension_exit_report(session)

    assert report["strategy_id"] == "trend_ride_extension"
    assert report["closed_trades"] == 2
    assert report["exit_reason_counts"]["take_profit"] == 1
    assert report["exit_reason_counts"]["shadow_forward_time_exit"] == 1
    assert report["median_hold_minutes"] >= 120


@pytest.mark.asyncio
async def test_darkflow_subportfolio_recommendations_whitelists_strong_and_blacklists_weak(session) -> None:
    opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    strong = [
        _darkflow_shadow_trade(
            key=f"strong-{index}",
            symbol="HYPEUSDT",
            direction="long",
            strategy_id="liquidity_sweep_reversal",
            strategy_name="扫损反转",
            market_state="liquidity_hunt_reversal",
            pnl=0.02,
            exit_reason="take_profit",
            opened_at=opened_at + timedelta(minutes=index),
        )
        for index in range(6)
    ]
    weak = [
        _darkflow_shadow_trade(
            key=f"weak-{index}",
            symbol="BTCUSDT",
            direction="long",
            strategy_id="trend_ride_extension",
            strategy_name="趋势延展",
            market_state="trend_extension",
            pnl=-0.015 if index < 8 else 0.003,
            exit_reason="shadow_forward_time_exit" if index < 8 else "take_profit",
            opened_at=opened_at + timedelta(hours=1, minutes=index),
        )
        for index in range(10)
    ]
    session.add_all(strong + weak)
    await session.commit()

    report = await darkflow_subportfolio_recommendations_report(session)

    by_group = {(row["strategy_id"], row["symbol"], row["direction"], row["market_state"]): row for row in report["rows"]}
    strong_row = by_group[("liquidity_sweep_reversal", "HYPEUSDT", "long", "liquidity_hunt_reversal")]
    weak_row = by_group[("trend_ride_extension", "BTCUSDT", "long", "trend_extension")]

    assert report["dimension"] == "strategy_id+symbol+direction+market_state"
    assert report["policy"]["report_only"] is True
    assert report["policy"]["opens_paper_trades"] is False
    assert report["policy"]["opens_live_orders"] is False
    assert strong_row["recommendation"] == "whitelist"
    assert strong_row["sampling_action"] == "prioritize"
    assert strong_row["main_path_action"] == "collect_more"
    assert weak_row["recommendation"] == "blacklist"
    assert weak_row["sampling_action"] == "block"
    assert weak_row["time_exit_share"] >= 0.8
    assert weak_row["main_path_action"] == "deweight"
    assert weak_row["main_path_weight_multiplier"] < 1.0
    assert any(item["strategy_id"] == "trend_ride_extension" and item["main_path_action"] == "deweight" for item in report["strategy_actions"])


def _darkflow_shadow_trade(
    *,
    key: str,
    symbol: str,
    direction: str,
    strategy_id: str,
    strategy_name: str,
    market_state: str,
    pnl: float,
    exit_reason: str,
    opened_at: datetime,
) -> ShadowPaperTrade:
    return ShadowPaperTrade(
        strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
        candidate_type="trade_candidate",
        candidate_key=key,
        signal_key=f"signal-{key}",
        symbol=symbol,
        timeframe="short",
        direction=direction,
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=103.0,
        position_size=1.0,
        status="closed",
        pnl=pnl,
        exit_reason=exit_reason,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=120),
        context={
            "candidate_snapshot": {
                "strategy_id": strategy_id,
                "strategy_name": strategy_name,
                "market_state": market_state,
            },
            "shadow_plan_fingerprint": key,
        },
    )
