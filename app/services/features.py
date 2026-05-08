from __future__ import annotations

from typing import Any


def summarize_signal_payload(payload: dict[str, Any], indicator: str) -> dict[str, Any]:
    klines = payload.get("klines") or []
    indicator_payload = payload.get(indicator)
    summary: dict[str, Any] = {
        "kline_count": len(klines),
        "first_kline_ts": klines[0][0] if klines else None,
        "last_kline_ts": klines[-1][0] if klines else None,
        "payload_keys": sorted(payload.keys()),
        "indicator": indicator,
        "indicator_item_count": _safe_len(indicator_payload),
    }
    if klines:
        last = klines[-1]
        summary["last_close"] = last[2] if len(last) > 2 else None
        summary["last_high"] = last[4] if len(last) > 4 else None
        summary["last_low"] = last[3] if len(last) > 3 else None
    return summary


def _safe_len(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, dict, str)):
        return len(value)
    return None

