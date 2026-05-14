from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import ExperimentRun, PriceSnapshot, ShadowPaperTrade
from app.services.shadow_paper import SHADOW_FEE_RATE, mark_shadow_paper_trades, shadow_paper_scan, shadow_paper_stats


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
    assert stats["policy"]["uses_fee_and_slippage"] is True


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
                context={},
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
                context={},
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
                context={},
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
    assert candidate_a["win_rate"] == 0.5
    assert candidate_a["profit_factor"] == 2.0
    assert candidate_a["max_drawdown"] > 0
    assert stats["by_symbol"][0]["symbol"] in {"BTCUSDT", "ETHUSDT"}


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
