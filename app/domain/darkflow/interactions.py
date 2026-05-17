from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


def normalize_klines(raw_klines: Iterable[Any]) -> list[Candle]:
    rows: list[Candle] = []
    for raw in raw_klines:
        candle = parse_candle(raw)
        if candle is not None:
            rows.append(candle)
    rows.sort(key=lambda item: item.ts)
    return rows


def parse_candle(raw: Any) -> Candle | None:
    if isinstance(raw, dict):
        ts = parse_timestamp(raw.get("timestamp") or raw.get("ts") or raw.get("time") or raw.get("open_time"))
        open_price = number(raw.get("open") or raw.get("o"))
        close = number(raw.get("close") or raw.get("c"))
        high = number(raw.get("high") or raw.get("h"))
        low = number(raw.get("low") or raw.get("l"))
    elif isinstance(raw, (list, tuple)) and len(raw) >= 5:
        ts = parse_timestamp(raw[0])
        open_price = number(raw[1])
        close = number(raw[2])
        low = number(raw[3])
        high = number(raw[4])
    else:
        return None
    if ts is None or None in (open_price, close, high, low):
        return None
    high_value = max(float(high), float(low), float(open_price), float(close))
    low_value = min(float(high), float(low), float(open_price), float(close))
    return Candle(ts=ts, open=float(open_price), high=high_value, low=low_value, close=float(close))


def playbook_for_zone(zone: dict[str, Any], interaction_type: str, *, playbooks: Iterable[Any]) -> str:
    indicator = str(zone["indicator"])
    family = str(zone["family"])
    if interaction_type == "wick_pierce_reclaim" and family == "liquidity":
        return "liquidity_sweep_reversal"
    if family in {"cost_structure", "volume_profile"}:
        return "pullback_to_cost"
    if family in {"structure_break", "orderflow"}:
        return "breakout_confirmation"
    if family == "vacuum":
        return "vacuum_acceleration"
    if family == "lifecycle":
        return "exhaustion_exit_filter"
    for playbook in playbooks:
        if indicator in playbook.entry_indicators:
            return playbook.key
    return "darkflow_zone_reaction"


def playbook_display_name(key: str, *, playbooks: Iterable[Any]) -> str:
    for playbook in playbooks:
        if playbook.key == key:
            return playbook.display_name
    return key


def playbook_blockers(key: str, *, playbooks: Iterable[Any]) -> tuple[str, ...]:
    for playbook in playbooks:
        if playbook.key == key:
            return playbook.blocker_indicators
    return ()


def quality_grade(score: float) -> str:
    if score >= 75:
        return "A"
    if score >= 60:
        return "B"
    if score >= 45:
        return "C"
    return "D"


def zone_key(**payload: Any) -> str:
    raw = json.dumps(json_safe(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def interaction_key(zone_key_value: str, interaction_type: str, event_ts: datetime, *, schema: str) -> str:
    raw = f"{schema}:{zone_key_value}:{interaction_type}:{aware(event_ts).isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return aware(value)
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw = raw / 1000.0
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str) and value:
        try:
            return aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
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


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return aware(value).isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value
