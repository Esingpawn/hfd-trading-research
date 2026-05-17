from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


FROZEN_ENTRY_PLAN_TYPE = "frozen_darkflow_v2_entry_plan"
DEFAULT_ENTRY_PLAN_VALID_BARS = 12
DEFAULT_ENTRY_PLAN_TOLERANCE_PCT = 0.006
DEFAULT_ENTRY_PLAN_DRIFT_LIMIT_PCT = 0.003


def build_frozen_entry_plan(
    *,
    direction: str,
    interaction_type: str,
    interval: str | None,
    event_ts: datetime,
    context: dict[str, Any],
    entry: float,
    stop: float,
    target: float,
    invalidation_price: float | None,
) -> dict[str, Any]:
    hold_bars = int(context.get("hold_bars") or 0)
    entry_range = entry_range_from_context(
        direction=direction,
        context=context,
        entry=entry,
        stop=stop,
        target=target,
    )
    valid_until = entry_plan_valid_until(event_ts=event_ts, interval=interval, max_hold_bars=hold_bars)
    invalidation = float(invalidation_price) if isinstance(invalidation_price, (int, float)) else stop
    return {
        "plan_type": FROZEN_ENTRY_PLAN_TYPE,
        "status": "frozen",
        "state": "frozen",
        "trigger": entry_trigger(interaction_type),
        "planned_entry": entry,
        "planned_stop": stop,
        "take_profit_levels": take_profit_levels(context=context, target=target),
        "invalidation_price": invalidation,
        "max_hold_bars": hold_bars,
        "frozen_at": iso(event_ts),
        "valid_until": iso(valid_until),
        "entry_reference_price": entry,
        "entry_range": entry_range,
        "entry_tolerance_pct": DEFAULT_ENTRY_PLAN_TOLERANCE_PCT,
        "drift_limit_pct": DEFAULT_ENTRY_PLAN_DRIFT_LIMIT_PCT,
        "invalidation_rules": [
            "price_crosses_invalidation",
            "valid_until_passed",
            "entry_range_missed",
            "reward_shape_changed",
        ],
        "used_for_outcome_tracking": True,
    }


def take_profit_levels(*, context: dict[str, Any], target: float) -> list[dict[str, Any]]:
    target_plan = context.get("target_plan") or {}
    return [
        {
            "label": "TP1",
            "price": target,
            "source": target_plan.get("model") or context.get("target_model") or "interaction_target",
        }
    ]


def entry_range_from_context(
    *,
    direction: str,
    context: dict[str, Any],
    entry: float,
    stop: float,
    target: float,
) -> dict[str, Any]:
    zone = context.get("zone") if isinstance(context.get("zone"), dict) else {}
    lower = number(zone.get("lower_price")) if isinstance(zone, dict) else None
    upper = number(zone.get("upper_price")) if isinstance(zone, dict) else None
    if lower is not None and upper is not None:
        source = "source_darkflow_zone"
        lower_value, upper_value = sorted((float(lower), float(upper)))
    else:
        source = "entry_reference_tolerance"
        width = abs(entry) * DEFAULT_ENTRY_PLAN_TOLERANCE_PCT
        lower_value = entry - width
        upper_value = entry + width
    clipped = clip_entry_range(
        direction,
        lower=lower_value,
        upper=upper_value,
        entry=entry,
        stop=stop,
        target=target,
    )
    if clipped is None:
        width = abs(entry) * DEFAULT_ENTRY_PLAN_TOLERANCE_PCT
        clipped = clip_entry_range(
            direction,
            lower=entry - width,
            upper=entry + width,
            entry=entry,
            stop=stop,
            target=target,
        ) or (entry - width, entry + width)
        source = "entry_reference_tolerance"
    return {
        "lower": round(float(clipped[0]), 10),
        "upper": round(float(clipped[1]), 10),
        "source": source,
    }


def clip_entry_range(
    direction: str,
    *,
    lower: float,
    upper: float,
    entry: float,
    stop: float,
    target: float,
) -> tuple[float, float] | None:
    if lower <= 0 or upper <= 0 or lower >= upper:
        return None
    epsilon = max(abs(entry) * 1e-9, 1e-9)
    if direction == "long":
        lower = max(lower, stop + epsilon)
        upper = min(upper, target - epsilon)
    elif direction == "short":
        lower = max(lower, target + epsilon)
        upper = min(upper, stop - epsilon)
    else:
        return None
    if lower >= upper or not (lower <= entry <= upper):
        return None
    return lower, upper


def entry_plan_valid_until(*, event_ts: datetime, interval: str | None, max_hold_bars: int) -> datetime:
    start = aware(event_ts)
    bars = max(1, int(max_hold_bars or DEFAULT_ENTRY_PLAN_VALID_BARS))
    validity = interval_delta(interval) * bars
    validity = max(validity, timedelta(hours=2))
    validity = min(validity, timedelta(hours=72))
    return start + validity


def interval_delta(interval: str | None) -> timedelta:
    raw = str(interval or "").strip().lower()
    if len(raw) >= 2 and raw[:-1].isdigit():
        count = int(raw[:-1])
        unit = raw[-1]
        if unit == "m":
            return timedelta(minutes=count)
        if unit == "h":
            return timedelta(hours=count)
        if unit == "d":
            return timedelta(days=count)
    return timedelta(minutes=30)


def entry_trigger(interaction_type: str) -> str:
    if interaction_type == "wick_pierce_reclaim":
        return "wick_pierce_reclaim_confirmed"
    if interaction_type == "first_touch":
        return "first_touch_zone_reaction"
    if interaction_type == "body_break":
        return "body_break_confirmation"
    return interaction_type


def candidate_plan_openable(
    *,
    direction: str,
    entry_price: float,
    stop_price: float,
    target_price: float,
    setup_time: datetime | None,
    now: datetime,
    max_candidate_age_hours: float,
) -> str | None:
    if direction not in {"long", "short"}:
        return "unsupported_direction"
    if entry_price <= 0 or stop_price <= 0 or target_price <= 0:
        return "invalid_plan_prices"
    if direction == "long" and not (stop_price < entry_price < target_price):
        return "invalid_long_reward_shape"
    if direction == "short" and not (target_price < entry_price < stop_price):
        return "invalid_short_reward_shape"
    aware_setup = aware(setup_time) if setup_time else None
    if aware_setup and max_candidate_age_hours > 0:
        if aware(now) - aware_setup > timedelta(hours=float(max_candidate_age_hours)):
            return "stale_candidate"
    return None


def within_entry_tolerance(price: float, planned_entry: float, *, entry_tolerance_pct: float) -> bool:
    if planned_entry <= 0:
        return False
    return abs(price - planned_entry) / planned_entry <= max(0.0, float(entry_tolerance_pct))


def entry_plan_state(
    *,
    plan: dict[str, Any],
    direction: str,
    fallback_entry: float,
    fallback_stop: float,
    fallback_target: float,
    mark_price: float,
    now: datetime,
    entry_tolerance_pct: float,
) -> dict[str, Any]:
    state = base_entry_plan_state(
        plan=plan,
        direction=direction,
        fallback_entry=fallback_entry,
        fallback_stop=fallback_stop,
        fallback_target=fallback_target,
        now=now,
        entry_tolerance_pct=entry_tolerance_pct,
        mark_price=float(mark_price),
        missing_price=False,
    )
    valid_until = parse_iso_datetime(state.get("valid_until"))
    if valid_until is not None and aware(now) > valid_until:
        state.update({"state": "expired", "reason": "valid_until_passed"})
        return state
    shape_error = entry_plan_shape_error(
        direction,
        entry=state["planned_entry"],
        stop=state["planned_stop"],
        target=state["target_price"],
        lower=state["entry_range"]["lower"],
        upper=state["entry_range"]["upper"],
    )
    if shape_error:
        state.update({"state": "invalid_shape", "reason": shape_error})
        return state
    if price_invalidated(direction, mark_price=mark_price, invalidation=state["invalidation_price"], stop=state["planned_stop"]):
        state.update({"state": "invalidated", "reason": "price_crosses_invalidation"})
        return state
    if state["entry_range"]["lower"] <= mark_price <= state["entry_range"]["upper"]:
        state.update({"state": "triggered", "reason": "mark_price_inside_frozen_entry_range"})
        return state
    if entry_range_missed(direction, mark_price=mark_price, lower=state["entry_range"]["lower"], upper=state["entry_range"]["upper"]):
        state.update({"state": "missed", "reason": "entry_range_missed"})
        return state
    return state


def missing_price_entry_plan_state(
    *,
    plan: dict[str, Any],
    direction: str,
    fallback_entry: float,
    fallback_stop: float,
    fallback_target: float,
    now: datetime,
    entry_tolerance_pct: float,
) -> dict[str, Any]:
    state = base_entry_plan_state(
        plan=plan,
        direction=direction,
        fallback_entry=fallback_entry,
        fallback_stop=fallback_stop,
        fallback_target=fallback_target,
        now=now,
        entry_tolerance_pct=entry_tolerance_pct,
        mark_price=None,
        missing_price=True,
    )
    valid_until = parse_iso_datetime(state.get("valid_until"))
    if valid_until is not None and aware(now) > valid_until:
        state.update({"state": "expired", "reason": "valid_until_passed"})
    return state


def base_entry_plan_state(
    *,
    plan: dict[str, Any],
    direction: str,
    fallback_entry: float,
    fallback_stop: float,
    fallback_target: float,
    now: datetime,
    entry_tolerance_pct: float,
    mark_price: float | None,
    missing_price: bool,
) -> dict[str, Any]:
    planned_entry = number(plan.get("planned_entry")) or float(fallback_entry)
    planned_stop = number(plan.get("planned_stop")) or float(fallback_stop)
    target = target_price_from_plan(plan) or float(fallback_target)
    invalidation = number(plan.get("invalidation_price")) or planned_stop
    lower, upper, range_source = entry_range(
        plan,
        direction=direction,
        planned_entry=planned_entry,
        planned_stop=planned_stop,
        target_price=target,
        entry_tolerance_pct=entry_tolerance_pct,
    )
    valid_until = parse_iso_datetime(plan.get("valid_until"))
    return {
        "state": "missing_price" if missing_price else "waiting",
        "reason": "missing_latest_price" if missing_price else "awaiting_frozen_entry_range",
        "plan_type": str(plan.get("plan_type") or "legacy_entry_plan"),
        "evaluated_at": iso(now),
        "mark_price": mark_price,
        "planned_entry": planned_entry,
        "planned_stop": planned_stop,
        "target_price": target,
        "invalidation_price": invalidation,
        "entry_range": {"lower": lower, "upper": upper, "source": range_source},
        "valid_until": iso(valid_until),
    }


def entry_range(
    plan: dict[str, Any],
    *,
    direction: str,
    planned_entry: float,
    planned_stop: float,
    target_price: float,
    entry_tolerance_pct: float,
) -> tuple[float, float, str]:
    raw = plan.get("entry_range") if isinstance(plan.get("entry_range"), dict) else {}
    lower = number(raw.get("lower"))
    upper = number(raw.get("upper"))
    if lower is not None and upper is not None and lower > 0 and upper > 0 and lower < upper:
        return float(lower), float(upper), str(raw.get("source") or "frozen_entry_range")
    tolerance = max(0.0, float(entry_tolerance_pct))
    width = planned_entry * tolerance
    lower = planned_entry - width
    upper = planned_entry + width
    epsilon = max(abs(planned_entry) * 1e-9, 1e-9)
    if direction == "long":
        lower = max(lower, planned_stop + epsilon)
        upper = min(upper, target_price - epsilon)
    elif direction == "short":
        lower = max(lower, target_price + epsilon)
        upper = min(upper, planned_stop - epsilon)
    return lower, upper, "runtime_entry_tolerance_fallback"


def entry_plan_shape_error(
    direction: str,
    *,
    entry: float,
    stop: float,
    target: float,
    lower: float,
    upper: float,
) -> str | None:
    epsilon = max(abs(entry) * 1e-8, 1e-8)
    if direction not in {"long", "short"}:
        return "unsupported_direction"
    if min(entry, stop, target, lower, upper) <= 0 or lower >= upper:
        return "invalid_plan_prices"
    if direction == "long" and not (stop < lower and lower - epsilon <= entry <= upper + epsilon and upper < target):
        return "invalid_long_frozen_entry_range"
    if direction == "short" and not (target < lower and lower - epsilon <= entry <= upper + epsilon and upper < stop):
        return "invalid_short_frozen_entry_range"
    return None


def price_invalidated(direction: str, *, mark_price: float, invalidation: float, stop: float) -> bool:
    boundary = invalidation if invalidation > 0 else stop
    if direction == "long":
        return mark_price <= boundary or mark_price <= stop
    if direction == "short":
        return mark_price >= boundary or mark_price >= stop
    return True


def entry_range_missed(direction: str, *, mark_price: float, lower: float, upper: float) -> bool:
    if direction == "long":
        return mark_price > upper
    if direction == "short":
        return mark_price < lower
    return False


def target_price_from_plan(plan: dict[str, Any]) -> float | None:
    levels = plan.get("take_profit_levels")
    if isinstance(levels, list) and levels:
        first = levels[0]
        if isinstance(first, dict):
            return number(first.get("price"))
    return None


def parse_iso_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return aware(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return aware(value).isoformat() if isinstance(value, datetime) else None
