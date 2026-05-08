from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import PaperTrade, PriceSnapshot, SignalObservation, StrategyDecision
from app.services.paper_review import paper_trade_review


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
async def test_paper_trade_review_returns_decision_signals_and_current_state(session) -> None:
    opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    decision = StrategyDecision(
        strategy_name="multi_timeframe_cost_stack",
        strategy_version="0.2",
        symbol="BTCUSDT",
        asset_tier="core",
        direction="long",
        score=82.0,
        decision="open",
        reason={"explanation": ["long setup"]},
        risk_payload={
            "entry_price": 100.0,
            "score_breakdown": {"weighted_score": 78.5, "weight_mode": "governed"},
        },
        created_at=opened_at,
    )
    session.add(decision)
    await session.flush()
    trade = PaperTrade(
        strategy_decision_id=decision.id,
        strategy_name=decision.strategy_name,
        strategy_version=decision.strategy_version,
        symbol="BTCUSDT",
        asset_tier="core",
        direction="long",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=104.0,
        position_size=0.005,
        status="open",
        mfe=0.03,
        mae=-0.01,
        opened_at=opened_at,
    )
    session.add(trade)
    session.add(
        SignalObservation(
            strategy_decision_id=decision.id,
            symbol="BTCUSDT",
            asset_tier="core",
            signal_name="长期方向",
            signal_role="direction",
            direction="long",
            strength=25.0,
            timeframe="long",
            interval="4h",
            price_at_signal=100.0,
            strategy_decision="open",
            strategy_score=82.0,
            participated_in_score=True,
            score_before=0.0,
            score_after=25.0,
            labels={"return_4h": 0.04, "mfe": 0.05, "mae": -0.01},
            status="labeled",
            observed_at=opened_at,
        )
    )
    session.add(
        PriceSnapshot(
            symbol="BTCUSDT",
            price=103.0,
            raw_payload={},
            collected_at=opened_at + timedelta(minutes=30),
        )
    )
    await session.commit()

    review = await paper_trade_review(session, trade.id)

    assert review is not None
    assert review["trade"]["id"] == trade.id
    assert review["decision"]["score"] == 82.0
    assert review["current"]["return_pct"] == pytest.approx(0.03)
    assert review["review"]["signal_counts"]["helpful_4h"] == 1
    assert review["review"]["weight_mode"] == "governed"
    assert review["review"]["sample_status"] == "insufficient"
    assert "mark_open_trade" in review["review"]["next_actions"]


@pytest.mark.asyncio
async def test_paper_trade_review_returns_none_for_missing_trade(session) -> None:
    assert await paper_trade_review(session, "missing") is None
