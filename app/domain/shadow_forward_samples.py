from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def unique_shadow_plans(
    trades: list[Any],
    *,
    include_horizon: bool = False,
    include_opened_slot: bool = True,
    include_bucket_prefix: bool = True,
) -> list[Any]:
    best_by_plan: dict[str, Any] = {}
    for trade in trades:
        key = shadow_plan_fingerprint(
            trade,
            include_horizon=include_horizon,
            include_opened_slot=include_opened_slot,
            include_bucket_prefix=include_bucket_prefix,
        )
        current = best_by_plan.get(key)
        if current is None or shadow_plan_rank(trade) > shadow_plan_rank(current):
            best_by_plan[key] = trade
    return list(best_by_plan.values())


def shadow_plan_rank(trade: Any) -> tuple[int, datetime, str]:
    closed_rank = 1 if _status(trade) == "closed" and _number(getattr(trade, "pnl", None)) is not None else 0
    observed_at = getattr(trade, "closed_at", None) or getattr(trade, "opened_at", None) or datetime.min.replace(tzinfo=timezone.utc)
    return (closed_rank, _aware(observed_at), str(getattr(trade, "id", "")))


def shadow_plan_fingerprint(
    trade: Any,
    *,
    include_horizon: bool = False,
    include_opened_slot: bool = True,
    include_bucket_prefix: bool = True,
) -> str:
    context = _context(trade)
    explicit = context.get("shadow_plan_fingerprint")
    if explicit:
        return f"explicit:{explicit}"

    snapshot = candidate_snapshot(trade)
    entry = _number(snapshot.get("entry_price")) or _number(getattr(trade, "entry_price", None)) or 0.0
    stop = _number(snapshot.get("stop_price")) or _number(getattr(trade, "stop_loss", None)) or 0.0
    target = _number(snapshot.get("target_price")) or _number(getattr(trade, "take_profit", None)) or 0.0

    parts = [
        str(getattr(trade, "strategy_name", "")),
        str(snapshot.get("strategy_id") or getattr(trade, "strategy_name", "")),
        str(getattr(trade, "symbol", "")),
        str(getattr(trade, "timeframe", "")),
        str(getattr(trade, "direction", "")),
    ]
    if include_bucket_prefix:
        parts.insert(0, "bucket")
    if include_horizon:
        parts.append(shadow_plan_horizon(trade))
    if include_opened_slot:
        parts.append(_opened_hour_slot(getattr(trade, "opened_at", None)))
    parts.extend(
        [
            rounded_price_bucket(entry),
            rounded_price_bucket(stop),
            rounded_price_bucket(target),
        ]
    )
    return ":".join(parts)


def candidate_plan_fingerprint(candidate: Any) -> str | None:
    strategy_id = str(getattr(candidate, "strategy_id", "") or "")
    symbol = str(getattr(candidate, "symbol", "") or "")
    timeframe = str(getattr(candidate, "timeframe", "") or "")
    direction = str(getattr(candidate, "direction", "") or "")
    entry = _number(getattr(candidate, "entry_price", None)) or 0.0
    stop = _number(getattr(candidate, "stop_price", None)) or 0.0
    target = _number(getattr(candidate, "target_price", None)) or 0.0
    if not strategy_id or entry <= 0 or stop <= 0 or target <= 0:
        return None
    return ":".join(
        [
            strategy_id,
            symbol,
            timeframe,
            direction,
            rounded_price_bucket(entry),
            rounded_price_bucket(stop),
            rounded_price_bucket(target),
        ]
    )


def candidate_plan_fingerprint_from_trade(trade: Any) -> str | None:
    snapshot = candidate_snapshot(trade)
    strategy_id = str(snapshot.get("strategy_id") or "")
    timeframe = str(snapshot.get("timeframe") or getattr(trade, "timeframe", "") or "")
    entry = _number(snapshot.get("entry_price")) or _number(getattr(trade, "entry_price", None)) or 0.0
    stop = _number(snapshot.get("stop_price")) or _number(getattr(trade, "stop_loss", None)) or 0.0
    target = _number(snapshot.get("target_price")) or _number(getattr(trade, "take_profit", None)) or 0.0
    if not strategy_id or entry <= 0 or stop <= 0 or target <= 0:
        return None
    return ":".join(
        [
            strategy_id,
            str(getattr(trade, "symbol", "")),
            timeframe,
            str(getattr(trade, "direction", "")),
            rounded_price_bucket(entry),
            rounded_price_bucket(stop),
            rounded_price_bucket(target),
        ]
    )


def candidate_snapshot_matches_plan(candidate: Any, snapshot: dict[str, Any]) -> bool:
    if not snapshot:
        return False
    if str(snapshot.get("strategy_id") or "") != str(getattr(candidate, "strategy_id", "") or ""):
        return False
    if str(snapshot.get("timeframe") or "") != str(getattr(candidate, "timeframe", "") or ""):
        return False
    entry = _number(snapshot.get("entry_price"))
    stop = _number(snapshot.get("stop_price"))
    target = _number(snapshot.get("target_price"))
    if entry is None or stop is None or target is None:
        return False
    return (
        same_price_bucket(float(getattr(candidate, "entry_price", 0.0) or 0.0), entry)
        and same_price_bucket(float(getattr(candidate, "stop_price", 0.0) or 0.0), stop)
        and same_price_bucket(float(getattr(candidate, "target_price", 0.0) or 0.0), target)
    )


def candidate_snapshot(trade: Any) -> dict[str, Any]:
    snapshot = _context(trade).get("candidate_snapshot")
    return snapshot if isinstance(snapshot, dict) else {}


def shadow_plan_horizon(trade: Any) -> str:
    horizon = _context(trade).get("horizon")
    return str(horizon) if horizon else "live"


def rounded_price_bucket(price: float) -> str:
    if price >= 1000:
        digits = 0
    elif price >= 100:
        digits = 1
    elif price >= 10:
        digits = 2
    elif price >= 1:
        digits = 4
    else:
        digits = 6
    return f"{round(float(price), digits):.{digits}f}"


def same_price_bucket(left: float, right: float) -> bool:
    if left <= 0 or right <= 0:
        return False
    return rounded_price_bucket(left) == rounded_price_bucket(right)


def _context(trade: Any) -> dict[str, Any]:
    context = getattr(trade, "context", None)
    return context if isinstance(context, dict) else {}


def _opened_hour_slot(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return _aware(value).replace(minute=0, second=0, microsecond=0).isoformat()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _status(trade: Any) -> str:
    return str(getattr(trade, "status", "") or "").lower()


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed == parsed else None
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if parsed == parsed else None
    return None
