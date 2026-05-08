from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OrderIntent:
    mode: str
    symbol: str
    side: str
    quantity: float
    order_type: str = "market"
    requested_price: float | None = None
    strategy_decision_id: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    reason: str
    checks: list[dict[str, Any]]


def evaluate_order_safety(
    intent: OrderIntent,
    safety_state: Any,
    *,
    env_live_enabled: bool,
    daily_order_count: int = 0,
    daily_notional: float = 0.0,
) -> SafetyDecision:
    checks: list[dict[str, Any]] = []
    mode = intent.mode.lower()
    side = intent.side.lower()
    symbol = intent.symbol.upper()
    notional = _notional(intent.quantity, intent.requested_price)

    if mode not in {"paper", "live"}:
        return _reject("invalid_mode", checks, f"unsupported mode: {intent.mode}")
    checks.append(_check("mode", True, mode))

    if side not in {"buy", "sell", "long", "short"}:
        return _reject("invalid_side", checks, f"unsupported side: {intent.side}")
    checks.append(_check("side", True, side))

    if intent.quantity <= 0:
        return _reject("invalid_quantity", checks, "quantity must be greater than zero")
    checks.append(_check("quantity", True, intent.quantity))

    allowed_symbols = [item.upper() for item in (getattr(safety_state, "allowed_symbols", []) or [])]
    if allowed_symbols and symbol not in allowed_symbols:
        return _reject("symbol_not_allowed", checks, f"{symbol} is not in allowed_symbols")
    checks.append(_check("symbol", True, symbol))

    max_order_notional = float(getattr(safety_state, "max_order_notional", 0.0) or 0.0)
    if max_order_notional > 0 and notional is not None and notional > max_order_notional:
        return _reject("max_order_notional_exceeded", checks, f"{notional} exceeds {max_order_notional}")
    checks.append(_check("max_order_notional", True, {"notional": notional, "limit": max_order_notional}))

    if mode == "paper":
        return SafetyDecision(True, "paper_order_allowed", checks)

    if not env_live_enabled:
        return _reject("live_disabled_by_environment", checks, "LIVE_TRADING_ENABLED is false")
    checks.append(_check("env_live_enabled", True, True))

    if not getattr(safety_state, "live_trading_enabled", False):
        return _reject("live_disabled_by_safety_state", checks, "safety state has live_trading_enabled=false")
    checks.append(_check("state_live_enabled", True, True))

    if getattr(safety_state, "kill_switch_active", True):
        return _reject("kill_switch_active", checks, "kill switch is active")
    checks.append(_check("kill_switch", True, False))

    if getattr(safety_state, "manual_confirmation_required", True):
        return _reject("manual_confirmation_required", checks, "manual confirmation is required before live orders")
    checks.append(_check("manual_confirmation", True, False))

    if notional is None:
        return _reject("missing_order_notional", checks, "live orders require requested_price to calculate notional risk")
    checks.append(_check("order_notional", True, notional))

    if max_order_notional <= 0:
        return _reject("missing_max_order_notional", checks, "max_order_notional must be set before live orders")
    checks.append(_check("live_max_order_notional_configured", True, max_order_notional))

    max_daily_notional = float(getattr(safety_state, "max_daily_notional", 0.0) or 0.0)
    if max_daily_notional <= 0:
        return _reject("missing_max_daily_notional", checks, "max_daily_notional must be set before live orders")
    checks.append(_check("live_max_daily_notional_configured", True, max_daily_notional))

    max_daily_orders = int(getattr(safety_state, "max_daily_orders", 0) or 0)
    if max_daily_orders <= 0:
        return _reject("missing_max_daily_orders", checks, "max_daily_orders must be set before live orders")
    checks.append(_check("live_max_daily_orders_configured", True, max_daily_orders))

    if daily_order_count >= max_daily_orders:
        return _reject("max_daily_orders_exceeded", checks, f"{daily_order_count} reaches {max_daily_orders}")
    checks.append(_check("daily_order_count", True, {"current": daily_order_count, "limit": max_daily_orders}))

    projected_daily_notional = daily_notional + notional
    if projected_daily_notional > max_daily_notional:
        return _reject("max_daily_notional_exceeded", checks, f"{projected_daily_notional} exceeds {max_daily_notional}")
    checks.append(_check("daily_notional", True, {"current": daily_notional, "projected": projected_daily_notional, "limit": max_daily_notional}))

    return SafetyDecision(True, "live_order_allowed", checks)


def _notional(quantity: float, price: float | None) -> float | None:
    if price is None or price <= 0:
        return None
    return quantity * price


def _reject(reason: str, checks: list[dict[str, Any]], detail: str) -> SafetyDecision:
    checks.append(_check(reason, False, detail))
    return SafetyDecision(False, reason, checks)


def _check(name: str, passed: bool, detail: Any) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}
