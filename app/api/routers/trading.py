from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.application.trading import (
    get_trading_safety_state,
    list_trade_orders,
    list_trading_audit_logs,
    submit_trade_order,
    update_trading_safety_state,
)

router = APIRouter()


@router.get("/trading/safety")
async def trading_safety(session: SessionDep) -> dict[str, object]:
    return await get_trading_safety_state(session)


@router.patch("/trading/safety")
async def update_trading_safety(
    session: SessionDep,
    live_trading_enabled: bool | None = Query(default=None),
    kill_switch_active: bool | None = Query(default=None),
    manual_confirmation_required: bool | None = Query(default=None),
    max_order_notional: float | None = Query(default=None, ge=0),
    max_daily_notional: float | None = Query(default=None, ge=0),
    max_daily_orders: int | None = Query(default=None, ge=0),
    allowed_symbols: list[str] | None = Query(default=None),
    notes: str | None = Query(default=None),
    actor: str = Query(default="operator", min_length=1),
) -> dict[str, object]:
    return await update_trading_safety_state(
        session,
        live_trading_enabled=live_trading_enabled,
        kill_switch_active=kill_switch_active,
        manual_confirmation_required=manual_confirmation_required,
        max_order_notional=max_order_notional,
        max_daily_notional=max_daily_notional,
        max_daily_orders=max_daily_orders,
        allowed_symbols=allowed_symbols,
        notes=notes,
        actor=actor,
    )


@router.post("/trading/orders")
async def create_trade_order(
    session: SessionDep,
    mode: str = Query(default="paper", pattern="^(paper|live)$"),
    symbol: str = Query(..., min_length=3),
    side: str = Query(..., pattern="^(buy|sell|long|short)$"),
    quantity: float = Query(..., gt=0),
    order_type: str = Query(default="market", min_length=1),
    requested_price: float | None = Query(default=None, gt=0),
    strategy_decision_id: str | None = Query(default=None),
    idempotency_key: str | None = Query(default=None),
    actor: str = Query(default="operator", min_length=1),
) -> dict[str, object]:
    return await submit_trade_order(
        session,
        mode=mode,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        requested_price=requested_price,
        strategy_decision_id=strategy_decision_id,
        idempotency_key=idempotency_key,
        actor=actor,
    )


@router.get("/trading/orders")
async def trading_orders(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, object]]:
    return await list_trade_orders(session, limit=limit)


@router.get("/trading/audit")
async def trading_audit(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, object]]:
    return await list_trading_audit_logs(session, limit=limit)
