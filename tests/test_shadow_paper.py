from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import ExperimentRun, FeatureEvent, FeatureLabel, PaperTrade, PriceSnapshot, ShadowPaperTrade
from app.services.shadow_paper import (
    SHADOW_FEE_RATE,
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
