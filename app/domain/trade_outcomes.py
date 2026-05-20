from __future__ import annotations

from statistics import mean
from typing import Any


EXIT_REASON_LABELS = {
    "take_profit": "止盈",
    "target_hit": "止盈",
    "stop_loss": "止损",
    "trailing_stop": "移动止损",
    "shadow_forward_time_exit": "时间退场",
    "time_exit": "时间退场",
    "invalidated": "条件作废",
    "manual_close": "手动平仓",
}


def build_trade_outcome(trade: Any, *, source: str) -> dict[str, Any]:
    status = str(getattr(trade, "status", "") or "").lower()
    direction = str(getattr(trade, "direction", "") or "").lower()
    entry_price = _number(getattr(trade, "entry_price", None))
    exit_price = _number(getattr(trade, "exit_price", None))
    exit_reason = getattr(trade, "exit_reason", None)
    net_pnl = _number(getattr(trade, "pnl", None))
    r_multiple = _number(getattr(trade, "r_multiple", None))
    mfe = _number(getattr(trade, "mfe", None))
    mae = _number(getattr(trade, "mae", None))

    gross_pnl = _gross_pnl(direction, entry_price, exit_price)
    missing_fields = _missing_fields(
        status=status,
        exit_price=exit_price,
        exit_reason=exit_reason,
        net_pnl=net_pnl,
        r_multiple=r_multiple,
        mfe=mfe,
        mae=mae,
    )
    return {
        "source": source,
        "valid": not missing_fields,
        "missing_fields": missing_fields,
        "exit_reason": exit_reason,
        "exit_reason_label": EXIT_REASON_LABELS.get(str(exit_reason), str(exit_reason or "--")),
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "cost_impact": _cost_impact(gross_pnl, net_pnl),
        "r_multiple": r_multiple,
        "mfe": mfe,
        "mae": mae,
    }


def summarize_trade_outcomes(
    trades: list[Any],
    *,
    no_loss_profit_factor: float | None = float("inf"),
    drawdown_mode: str = "compound",
) -> dict[str, Any]:
    closed_all = [trade for trade in trades if _status(trade) == "closed"]
    open_trades = [trade for trade in trades if _status(trade) == "open"]
    valid_closed = [trade for trade in closed_all if _number(getattr(trade, "pnl", None)) is not None]
    invalid_closed = [trade for trade in closed_all if _number(getattr(trade, "pnl", None)) is None]
    ordered_closed = sorted(valid_closed, key=_closed_sort_key)
    pnl_values = [float(getattr(trade, "pnl")) for trade in ordered_closed]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    r_values = [
        float(value)
        for trade in ordered_closed
        if (value := _number(getattr(trade, "r_multiple", None))) is not None
    ]
    return {
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "closed_trades": len(closed_all),
        "valid_outcome_trades": len(valid_closed),
        "invalid_outcome_trades": len(invalid_closed),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": len(wins) / len(valid_closed) if valid_closed else None,
        "avg_pnl": mean(pnl_values) if pnl_values else None,
        "total_pnl": sum(pnl_values),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor(gross_profit, gross_loss, no_loss_value=no_loss_profit_factor),
        "max_drawdown": max_drawdown(pnl_values, mode=drawdown_mode),
        "avg_r_multiple": mean(r_values) if r_values else None,
        "best_trade": max(pnl_values) if pnl_values else None,
        "worst_trade": min(pnl_values) if pnl_values else None,
    }


def profit_factor(gross_profit: float, gross_loss: float, *, no_loss_value: float | None = float("inf")) -> float | None:
    if gross_loss:
        return gross_profit / gross_loss
    if gross_profit:
        return no_loss_value
    return None


def max_drawdown(returns: list[float], *, mode: str = "compound") -> float:
    if mode == "additive":
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for value in returns:
            equity += value
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return max_dd
    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        if peak:
            drawdown = max(drawdown, (peak - equity) / peak)
    return drawdown


def _gross_pnl(direction: str, entry_price: float | None, exit_price: float | None) -> float | None:
    if entry_price is None or exit_price is None or entry_price == 0:
        return None
    if direction == "short":
        return (entry_price - exit_price) / entry_price
    return (exit_price - entry_price) / entry_price


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _status(trade: Any) -> str:
    return str(getattr(trade, "status", "") or "").lower()


def _closed_sort_key(trade: Any) -> Any:
    return getattr(trade, "closed_at", None) or getattr(trade, "opened_at", None) or ""


def _cost_impact(gross_pnl: float | None, net_pnl: float | None) -> float | None:
    if gross_pnl is None or net_pnl is None:
        return None
    return gross_pnl - net_pnl


def _missing_fields(
    *,
    status: str,
    exit_price: float | None,
    exit_reason: Any,
    net_pnl: float | None,
    r_multiple: float | None,
    mfe: float | None,
    mae: float | None,
) -> list[str]:
    if status != "closed":
        return []
    missing: list[str] = []
    if exit_price is None:
        missing.append("exit_price")
    if not exit_reason:
        missing.append("exit_reason")
    if net_pnl is None:
        missing.append("pnl")
    if r_multiple is None:
        missing.append("r_multiple")
    if mfe is None:
        missing.append("mfe")
    if mae is None:
        missing.append("mae")
    return missing
