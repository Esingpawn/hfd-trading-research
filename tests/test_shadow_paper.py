from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import ExperimentRun, PriceSnapshot, ShadowPaperTrade
from app.services.shadow_paper import mark_shadow_paper_trades, shadow_paper_scan, shadow_paper_stats


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
    assert trades[0].candidate_type == "watchlist_segment"
    assert trades[0].status == "open"


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
    assert stats["closed_trades"] == 1
    assert stats["policy"]["opens_live_orders"] is False
