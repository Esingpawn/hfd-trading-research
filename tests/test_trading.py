import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.trading import (
    get_trading_safety_state,
    list_trade_orders,
    list_trading_audit_logs,
    submit_trade_order,
    update_trading_safety_state,
)
from app.db import Base


@pytest.fixture()
async def session(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trading.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_safety_state_blocks_live_trading(session) -> None:
    state = await get_trading_safety_state(session)
    order = await submit_trade_order(
        session,
        mode="live",
        symbol="BTCUSDT",
        side="buy",
        quantity=0.01,
        requested_price=100_000,
    )

    assert state["live_trading_enabled"] is False
    assert state["kill_switch_active"] is True
    assert order["status"] == "rejected"
    assert order["rejection_reason"] == "live_disabled_by_environment"


@pytest.mark.asyncio
async def test_paper_order_is_filled_and_audited(session) -> None:
    order = await submit_trade_order(
        session,
        mode="paper",
        symbol="ETHUSDT",
        side="buy",
        quantity=0.25,
        requested_price=4_000,
        idempotency_key="paper-eth-1",
    )
    same_order = await submit_trade_order(
        session,
        mode="paper",
        symbol="ETHUSDT",
        side="buy",
        quantity=0.25,
        requested_price=4_000,
        idempotency_key="paper-eth-1",
    )
    orders = await list_trade_orders(session)
    audit = await list_trading_audit_logs(session)

    assert order["status"] == "paper_filled"
    assert order["notional"] == 1000
    assert same_order["id"] == order["id"]
    assert len(orders) == 1
    assert any(item["event_type"] == "order_submitted" for item in audit)


@pytest.mark.asyncio
async def test_live_order_still_blocked_without_real_gateway(session, monkeypatch) -> None:
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    await update_trading_safety_state(
        session,
        live_trading_enabled=True,
        kill_switch_active=False,
        manual_confirmation_required=False,
        max_order_notional=500,
        max_daily_notional=1000,
        max_daily_orders=2,
        allowed_symbols=["BTCUSDT"],
        actor="test",
    )
    order = await submit_trade_order(
        session,
        mode="live",
        symbol="BTCUSDT",
        side="buy",
        quantity=0.001,
        requested_price=100_000,
    )

    assert order["status"] == "blocked"
    assert order["rejection_reason"] == "live trading gateway is not configured"


@pytest.mark.asyncio
async def test_symbol_allowlist_rejects_order(session) -> None:
    await update_trading_safety_state(session, allowed_symbols=["BTCUSDT"])
    order = await submit_trade_order(
        session,
        mode="paper",
        symbol="ETHUSDT",
        side="buy",
        quantity=1,
        requested_price=4000,
    )

    assert order["status"] == "rejected"
    assert order["rejection_reason"] == "symbol_not_allowed"
