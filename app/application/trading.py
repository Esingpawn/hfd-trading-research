from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.risk.trading_safety import OrderIntent, evaluate_order_safety
from app.infrastructure.trading_gateway import TradingGatewayError, build_trading_gateway
from app.models import TradeOrder, TradingAuditLog, TradingSafetyState


async def get_trading_safety_state(session: AsyncSession, *, scope: str = "global") -> dict[str, Any]:
    state = await _get_or_create_safety_state(session, scope=scope, commit_on_create=True)
    return _safety_payload(state)


async def update_trading_safety_state(
    session: AsyncSession,
    *,
    scope: str = "global",
    live_trading_enabled: bool | None = None,
    kill_switch_active: bool | None = None,
    manual_confirmation_required: bool | None = None,
    max_order_notional: float | None = None,
    max_daily_notional: float | None = None,
    max_daily_orders: int | None = None,
    allowed_symbols: list[str] | None = None,
    notes: str | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    state = await _get_or_create_safety_state(session, scope=scope, commit_on_create=True)
    before = _safety_payload(state)
    if live_trading_enabled is not None:
        state.live_trading_enabled = live_trading_enabled
    if kill_switch_active is not None:
        state.kill_switch_active = kill_switch_active
    if manual_confirmation_required is not None:
        state.manual_confirmation_required = manual_confirmation_required
    if max_order_notional is not None:
        state.max_order_notional = max_order_notional
    if max_daily_notional is not None:
        state.max_daily_notional = max_daily_notional
    if max_daily_orders is not None:
        state.max_daily_orders = max_daily_orders
    if allowed_symbols is not None:
        state.allowed_symbols = [item.upper() for item in allowed_symbols]
    if notes is not None:
        state.notes = notes
    state.updated_at = _utc_now()
    session.add(
        TradingAuditLog(
            event_type="safety_state_updated",
            actor=actor,
            safety_state_id=state.id,
            payload=_json_safe({"before": before, "after": _safety_payload(state)}),
        )
    )
    await session.commit()
    return _safety_payload(state)


async def submit_trade_order(
    session: AsyncSession,
    *,
    mode: str,
    symbol: str,
    side: str,
    quantity: float,
    order_type: str = "market",
    requested_price: float | None = None,
    strategy_decision_id: str | None = None,
    idempotency_key: str | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    if idempotency_key:
        existing = await _order_by_idempotency_key(session, idempotency_key)
        if existing is not None:
            return _order_payload(existing)
    settings = get_settings()
    state = await _get_or_create_safety_state(session)
    daily_usage = await _daily_live_usage(session)
    intent = OrderIntent(
        mode=mode.lower(),
        symbol=symbol.upper(),
        side=side.lower(),
        quantity=quantity,
        order_type=order_type.lower(),
        requested_price=requested_price,
        strategy_decision_id=strategy_decision_id,
        idempotency_key=idempotency_key,
    )
    safety_decision = evaluate_order_safety(
        intent,
        state,
        env_live_enabled=settings.live_trading_enabled,
        daily_order_count=daily_usage["orders"],
        daily_notional=daily_usage["notional"],
    )
    order = TradeOrder(
        mode=intent.mode,
        symbol=intent.symbol,
        side=intent.side,
        order_type=intent.order_type,
        quantity=intent.quantity,
        requested_price=intent.requested_price,
        notional=_notional(intent.quantity, intent.requested_price),
        strategy_decision_id=intent.strategy_decision_id,
        idempotency_key=intent.idempotency_key,
        client_order_id=_client_order_id(intent),
        request_payload={
            "mode": intent.mode,
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "order_type": intent.order_type,
            "requested_price": intent.requested_price,
        },
        safety_snapshot={
            "state": _json_safe(_safety_payload(state)),
            "decision": safety_decision.__dict__,
        },
        result_payload={},
    )
    if not safety_decision.allowed:
        order.status = "rejected"
        order.rejection_reason = safety_decision.reason
        session.add(order)
        session.add(
            TradingAuditLog(
                event_type="order_rejected",
                actor=actor,
                order_id=order.id,
                safety_state_id=state.id,
                payload={"reason": safety_decision.reason, "checks": safety_decision.checks},
            )
        )
        await session.commit()
        return _order_payload(order)

    session.add(order)
    session.add(
        TradingAuditLog(
            event_type="order_allowed",
            actor=actor,
            order_id=order.id,
                safety_state_id=state.id,
                payload={"mode": order.mode, "checks": safety_decision.checks},
        )
    )
    await session.flush()
    try:
        gateway_result = await build_trading_gateway(order.mode, settings.trading_gateway).submit_order(
            {**order.request_payload, "client_order_id": order.client_order_id}
        )
    except TradingGatewayError as exc:
        order.status = "blocked"
        order.rejection_reason = str(exc)
        order.result_payload = {"gateway_error": str(exc)}
        session.add(
            TradingAuditLog(
                event_type="order_gateway_blocked",
                actor=actor,
                order_id=order.id,
                safety_state_id=state.id,
                payload={"error": str(exc)},
            )
        )
    else:
        order.status = "submitted" if order.mode == "live" else "paper_filled"
        order.result_payload = gateway_result
        order.submitted_at = _utc_now()
        if order.mode == "paper":
            order.filled_at = order.submitted_at
        session.add(
            TradingAuditLog(
                event_type="order_submitted",
                actor=actor,
                order_id=order.id,
                safety_state_id=state.id,
                payload=gateway_result,
            )
        )
    await session.commit()
    return _order_payload(order)


async def list_trade_orders(session: AsyncSession, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = await session.execute(select(TradeOrder).order_by(TradeOrder.created_at.desc()).limit(limit))
    return [_order_payload(item) for item in rows.scalars()]


async def list_trading_audit_logs(session: AsyncSession, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = await session.execute(select(TradingAuditLog).order_by(TradingAuditLog.created_at.desc()).limit(limit))
    return [_audit_payload(item) for item in rows.scalars()]


async def _get_or_create_safety_state(
    session: AsyncSession,
    *,
    scope: str = "global",
    commit_on_create: bool = False,
) -> TradingSafetyState:
    rows = await session.execute(
        select(TradingSafetyState).where(TradingSafetyState.scope == scope).order_by(TradingSafetyState.created_at.desc()).limit(1)
    )
    state = rows.scalar_one_or_none()
    if state is not None:
        return state
    state = TradingSafetyState(scope=scope)
    session.add(state)
    await session.flush()
    session.add(
        TradingAuditLog(
            event_type="safety_state_created",
            actor="system",
            safety_state_id=state.id,
            payload=_json_safe(_safety_payload(state)),
        )
    )
    if commit_on_create:
        await session.commit()
    return state


async def _order_by_idempotency_key(session: AsyncSession, idempotency_key: str) -> TradeOrder | None:
    rows = await session.execute(
        select(TradeOrder).where(TradeOrder.idempotency_key == idempotency_key).order_by(TradeOrder.created_at.desc()).limit(1)
    )
    return rows.scalar_one_or_none()


def _safety_payload(state: TradingSafetyState) -> dict[str, Any]:
    return {
        "id": state.id,
        "scope": state.scope,
        "live_trading_enabled": state.live_trading_enabled,
        "kill_switch_active": state.kill_switch_active,
        "manual_confirmation_required": state.manual_confirmation_required,
        "max_order_notional": state.max_order_notional,
        "max_daily_notional": state.max_daily_notional,
        "max_daily_orders": state.max_daily_orders,
        "allowed_symbols": state.allowed_symbols,
        "notes": state.notes,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
    }


def _order_payload(order: TradeOrder) -> dict[str, Any]:
    return {
        "id": order.id,
        "mode": order.mode,
        "symbol": order.symbol,
        "side": order.side,
        "order_type": order.order_type,
        "status": order.status,
        "quantity": order.quantity,
        "requested_price": order.requested_price,
        "notional": order.notional,
        "strategy_decision_id": order.strategy_decision_id,
        "idempotency_key": order.idempotency_key,
        "client_order_id": order.client_order_id,
        "request_payload": order.request_payload,
        "safety_snapshot": order.safety_snapshot,
        "result_payload": order.result_payload,
        "rejection_reason": order.rejection_reason,
        "created_at": order.created_at,
        "submitted_at": order.submitted_at,
        "filled_at": order.filled_at,
        "canceled_at": order.canceled_at,
    }


def _audit_payload(item: TradingAuditLog) -> dict[str, Any]:
    return {
        "id": item.id,
        "event_type": item.event_type,
        "actor": item.actor,
        "order_id": item.order_id,
        "safety_state_id": item.safety_state_id,
        "payload": item.payload,
        "created_at": item.created_at,
    }


def _notional(quantity: float, price: float | None) -> float | None:
    if price is None or price <= 0:
        return None
    return quantity * price


def _client_order_id(intent: OrderIntent) -> str:
    if intent.idempotency_key:
        return f"hfd-{intent.mode}-{intent.idempotency_key}"
    return f"hfd-{intent.mode}-{int(_utc_now().timestamp() * 1000)}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _daily_live_usage(session: AsyncSession) -> dict[str, Any]:
    day_start = _utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await session.execute(
        select(func.count(TradeOrder.id), func.coalesce(func.sum(TradeOrder.notional), 0.0)).where(
            TradeOrder.mode == "live",
            TradeOrder.status.in_(["submitted", "filled", "partially_filled"]),
            TradeOrder.created_at >= day_start,
        )
    )
    count, notional = rows.one()
    return {"orders": int(count or 0), "notional": float(notional or 0.0)}


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value
