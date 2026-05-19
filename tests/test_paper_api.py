from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routers.paper import paper_trades
from app.db import Base
from app.models import PaperTrade


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
async def test_paper_trades_exposes_exit_reason_and_precise_pnl(session) -> None:
    opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add(
        PaperTrade(
            strategy_decision_id="decision-1",
            strategy_name="darkflow_v2",
            strategy_version="test",
            symbol="ETHUSDT",
            asset_tier="core",
            direction="long",
            entry_price=2106.62,
            stop_loss=2080.0,
            take_profit=2150.0,
            position_size=1.0,
            status="closed",
            exit_price=2149.5,
            exit_reason="take_profit",
            pnl=0.00006626729073112397,
            r_multiple=2.374752435035664,
            mfe=0.013253458146224794,
            mae=-0.0004129838319202756,
            opened_at=opened_at,
            closed_at=opened_at,
        )
    )
    await session.commit()

    rows = await paper_trades(session, limit=5)

    assert rows[0]["exit_reason"] == "take_profit"
    assert rows[0]["exit_price"] == 2149.5
    assert rows[0]["pnl"] == pytest.approx(0.00006626729073112397)
    assert rows[0]["mfe"] == pytest.approx(0.013253458146224794)
