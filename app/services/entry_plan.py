from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def entry_plan_compatibility(
    current_risk: dict[str, Any] | None,
    previous_risk: dict[str, Any] | None,
) -> dict[str, Any]:
    current_risk = current_risk or {}
    previous_risk = previous_risk or {}
    current_zone = _execution_zone(current_risk)
    previous_zone = _execution_zone(previous_risk)
    drift_limit = max(
        _float((current_risk.get("entry_plan") or {}).get("drift_limit_pct")) or 0.003,
        _float((previous_risk.get("entry_plan") or {}).get("drift_limit_pct")) or 0.003,
    )
    reasons: list[str] = []
    if not current_zone or not previous_zone:
        reasons.append("missing_execution_zone")
    else:
        overlap = _zone_overlap_ratio(current_zone, previous_zone)
        if overlap <= 0:
            reasons.append("execution_zone_changed")
    entry_drift = _relative_drift(_entry_reference(current_risk), _entry_reference(previous_risk))
    stop_drift = _relative_drift(_float(current_risk.get("stop_loss")), _float(previous_risk.get("stop_loss")))
    target_drift = _relative_drift(_float(current_risk.get("take_profit")), _float(previous_risk.get("take_profit")))
    if entry_drift is None:
        reasons.append("missing_entry_reference")
    elif entry_drift > drift_limit:
        reasons.append("entry_reference_drift")
    if stop_drift is None:
        reasons.append("missing_stop_loss")
    elif stop_drift > drift_limit:
        reasons.append("stop_loss_drift")
    if target_drift is None:
        reasons.append("missing_take_profit")
    elif target_drift > drift_limit:
        reasons.append("take_profit_drift")
    return {
        "compatible": not reasons,
        "reasons": reasons,
        "drift_limit_pct": drift_limit,
        "entry_drift_pct": entry_drift,
        "stop_drift_pct": stop_drift,
        "target_drift_pct": target_drift,
        "execution_zone_overlap_ratio": _zone_overlap_ratio(current_zone, previous_zone)
        if current_zone and previous_zone
        else 0.0,
    }


def entry_plan_is_expired(plan: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    if not plan:
        return True
    raw = plan.get("valid_until")
    if not isinstance(raw, str):
        return True
    try:
        valid_until = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    if valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=timezone.utc)
    return now > valid_until.astimezone(timezone.utc)


def _execution_zone(risk: dict[str, Any]) -> tuple[float, float] | None:
    zone = risk.get("execution_zone") or {}
    lower = _float(zone.get("lower"))
    upper = _float(zone.get("upper"))
    if lower is None or upper is None or lower > upper:
        return None
    return lower, upper


def _entry_reference(risk: dict[str, Any]) -> float | None:
    plan = risk.get("entry_plan") or {}
    return (
        _float(plan.get("entry_reference_price"))
        or _float(risk.get("entry_price"))
        or _float(risk.get("price_at_signal"))
    )


def _zone_overlap_ratio(left: tuple[float, float], right: tuple[float, float]) -> float:
    lower = max(left[0], right[0])
    upper = min(left[1], right[1])
    overlap = max(upper - lower, 0.0)
    base = min(left[1] - left[0], right[1] - right[0])
    return overlap / base if base > 0 else 0.0


def _relative_drift(left: float | None, right: float | None) -> float | None:
    if left is None or right is None or right == 0:
        return None
    return abs(left - right) / abs(right)


def _float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
