from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import EXPERIMENT_INDICATORS, HFD_INDICATORS
from app.models import SignalSnapshot


HORIZON_BARS: dict[str, int] = {"30m": 1, "1h": 2, "4h": 8, "24h": 48}
DEFAULT_HORIZON = "4h"
DEFAULT_LIMIT_PER_SERIES = 80


@dataclass(frozen=True)
class FeatureEvent:
    symbol: str
    timeframe: str
    indicator: str
    direction: str
    timestamp: int
    price: float
    strength: float
    subtype: str
    return_pct: float
    mfe: float
    mae: float


async def experiment_feature_effectiveness(
    session: AsyncSession,
    *,
    horizon: str = DEFAULT_HORIZON,
    min_samples: int = 5,
    limit_per_series: int = DEFAULT_LIMIT_PER_SERIES,
) -> dict[str, Any]:
    horizon_bars = HORIZON_BARS[horizon]
    snapshots = await _latest_experiment_snapshots(session)
    events: list[FeatureEvent] = []
    payload_fingerprints: dict[str, set[str]] = {}
    series_count: dict[str, int] = {}
    for snapshot in snapshots:
        series_count[snapshot.indicator] = series_count.get(snapshot.indicator, 0) + 1
        raw_payload = snapshot.raw_payload or {}
        payload_fingerprints.setdefault(snapshot.indicator, set()).add(_payload_fingerprint(raw_payload))
        events.extend(
            _events_from_snapshot(
                snapshot,
                horizon_bars=horizon_bars,
                limit_per_series=limit_per_series,
            )
        )
    rows = [
        _indicator_row(
            indicator,
            events=[event for event in events if event.indicator == indicator],
            series_count=series_count.get(indicator, 0),
            unique_payload_shapes=len(payload_fingerprints.get(indicator, set())),
            min_samples=min_samples,
        )
        for indicator in EXPERIMENT_INDICATORS
    ]
    return {
        "horizon": horizon,
        "min_samples": min_samples,
        "limit_per_series": limit_per_series,
        "series_count": len(snapshots),
        "event_count": len(events),
        "policy": {
            "used_for_execution_weights": False,
            "used_for_opening_decisions": False,
            "reason": "Feature tests are research-only until promoted through paper A/B validation.",
        },
        "indicators": rows,
        "by_family": _group_rows(rows, "family"),
        "by_timeframe": _timeframe_rows(events, min_samples=min_samples),
    }


async def _latest_experiment_snapshots(session: AsyncSession) -> list[SignalSnapshot]:
    rows = await session.execute(
        select(SignalSnapshot).where(SignalSnapshot.indicator.in_(EXPERIMENT_INDICATORS))
    )
    snapshots = rows.scalars().all()
    latest: dict[tuple[str, str, str], SignalSnapshot] = {}
    for snapshot in snapshots:
        key = (snapshot.symbol, snapshot.timeframe, snapshot.indicator)
        current = latest.get(key)
        if current is None or snapshot.created_at > current.created_at:
            latest[key] = snapshot
    return list(latest.values())


def _events_from_snapshot(
    snapshot: SignalSnapshot,
    *,
    horizon_bars: int,
    limit_per_series: int,
) -> list[FeatureEvent]:
    raw_payload = snapshot.raw_payload or {}
    klines = _normalize_klines(raw_payload.get("klines") or [])
    if len(klines) <= horizon_bars + 1:
        return []
    by_ts = {item["ts"]: index for index, item in enumerate(klines)}
    features = _feature_items(snapshot.indicator, raw_payload)
    rows: list[FeatureEvent] = []
    for item in features[-limit_per_series:]:
        ts = _feature_ts(item)
        index = _nearest_kline_index(klines, by_ts, ts)
        if index is None or index + horizon_bars >= len(klines):
            continue
        base_price = _feature_price(item) or klines[index]["close"]
        if not base_price:
            continue
        direction = _feature_direction(item)
        future = klines[index + 1 : index + horizon_bars + 1]
        close_return = _directional_return(direction, base_price, future[-1]["close"])
        path_returns = _path_returns(direction, base_price, future)
        rows.append(
            FeatureEvent(
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                indicator=snapshot.indicator,
                direction=direction,
                timestamp=klines[index]["ts"],
                price=base_price,
                strength=_feature_strength(item),
                subtype=str(item.get("type") or item.get("direction") or "unknown"),
                return_pct=close_return,
                mfe=max(path_returns),
                mae=min(path_returns),
            )
        )
    return rows


def _indicator_row(
    indicator: str,
    *,
    events: list[FeatureEvent],
    series_count: int,
    unique_payload_shapes: int,
    min_samples: int,
) -> dict[str, Any]:
    config = HFD_INDICATORS[indicator]
    sample_count = len(events)
    values = [event.return_pct for event in events]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    avg_return = mean(values) if values else None
    win_rate = len(wins) / sample_count if sample_count else None
    status = _status(sample_count, avg_return, win_rate, min_samples)
    return {
        "key": indicator,
        "hfd_name": config.hfd_name,
        "english_name": config.english_name,
        "family": config.family,
        "sample_count": sample_count,
        "series_count": series_count,
        "unique_payload_shapes": unique_payload_shapes,
        "win_rate": win_rate,
        "avg_return": avg_return,
        "median_return": _median(values),
        "profit_factor": gross_win / gross_loss if gross_loss else (999.0 if gross_win else None),
        "avg_mfe": mean([event.mfe for event in events]) if events else None,
        "avg_mae": mean([event.mae for event in events]) if events else None,
        "long_count": len([event for event in events if event.direction == "long"]),
        "short_count": len([event for event in events if event.direction == "short"]),
        "status": status,
        "noise_risk": _noise_risk(sample_count, unique_payload_shapes, avg_return, win_rate),
        "recommendation": _recommendation(status),
        "used_for_execution_weights": False,
        "used_for_opening_decisions": False,
    }


def _group_rows(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get(key) or "unknown"), []).append(row)
    result = []
    for name, items in groups.items():
        samples = sum(int(item["sample_count"] or 0) for item in items)
        weighted = [
            (float(item["avg_return"]), int(item["sample_count"]))
            for item in items
            if item.get("avg_return") is not None and item.get("sample_count")
        ]
        result.append(
            {
                "name": name,
                "indicator_count": len(items),
                "sample_count": samples,
                "avg_return": _weighted_average(weighted),
                "ready_count": len([item for item in items if item["status"] == "candidate"]),
            }
        )
    return sorted(result, key=lambda row: (row["avg_return"] or -999, row["sample_count"]), reverse=True)


def _timeframe_rows(events: list[FeatureEvent], *, min_samples: int) -> list[dict[str, Any]]:
    groups: dict[str, list[FeatureEvent]] = {}
    for event in events:
        groups.setdefault(event.timeframe, []).append(event)
    rows = []
    for timeframe, items in groups.items():
        values = [item.return_pct for item in items]
        avg_return = mean(values) if values else None
        win_rate = len([value for value in values if value > 0]) / len(values) if values else None
        rows.append(
            {
                "timeframe": timeframe,
                "sample_count": len(items),
                "win_rate": win_rate,
                "avg_return": avg_return,
                "status": _status(len(items), avg_return, win_rate, min_samples),
            }
        )
    return sorted(rows, key=lambda row: row["sample_count"], reverse=True)


def _feature_items(indicator: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get(indicator)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    for key in ("order_blocks", "volume_profile"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _normalize_klines(raw_klines: list[Any]) -> list[dict[str, float]]:
    rows = []
    for raw in raw_klines:
        if not isinstance(raw, list) or len(raw) < 5:
            continue
        rows.append(
            {
                "ts": int(raw[0]),
                "open": float(raw[1]),
                "close": float(raw[2]),
                "low": float(raw[3]),
                "high": float(raw[4]),
            }
        )
    return rows


def _feature_ts(item: dict[str, Any]) -> int | None:
    value = item.get("timestamp") or item.get("start_time") or item.get("origin_ts") or item.get("end_time")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return _parse_timestamp_ms(value)
    return None


def _feature_price(item: dict[str, Any]) -> float | None:
    for key in ("price", "level_price", "avg_price", "top_price", "bottom_price"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _feature_direction(item: dict[str, Any]) -> str:
    raw = str(item.get("type") or item.get("direction") or "").lower()
    if "bull" in raw or "accumulation" in raw or "long" in raw:
        return "long"
    if "bear" in raw or "distribution" in raw or "short" in raw:
        return "short"
    return "long"


def _feature_strength(item: dict[str, Any]) -> float:
    for key in ("purity", "volume", "total_vol", "buy_vol", "sell_vol"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _nearest_kline_index(
    klines: list[dict[str, float]],
    by_ts: dict[int, int],
    ts: int | None,
) -> int | None:
    if ts is None:
        return None
    if ts in by_ts:
        return by_ts[ts]
    best_index = None
    best_delta = None
    for index, kline in enumerate(klines):
        delta = abs(int(kline["ts"]) - ts)
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_index = index
    return best_index


def _directional_return(direction: str, entry: float, price: float) -> float:
    if not entry:
        return 0.0
    if direction == "short":
        return (entry - price) / entry
    return (price - entry) / entry


def _path_returns(direction: str, entry: float, klines: list[dict[str, float]]) -> list[float]:
    if direction == "short":
        return [
            _directional_return(direction, entry, point["low"])
            for point in klines
        ] + [
            _directional_return(direction, entry, point["high"])
            for point in klines
        ]
    return [
        _directional_return(direction, entry, point["high"])
        for point in klines
    ] + [
        _directional_return(direction, entry, point["low"])
        for point in klines
    ]


def _parse_timestamp_ms(value: str) -> int | None:
    normalized = value.strip().replace("Z", "+00:00")
    formats = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in formats:
            try:
                parsed = datetime.strptime(normalized, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _status(sample_count: int, avg_return: float | None, win_rate: float | None, min_samples: int) -> str:
    if sample_count < min_samples or avg_return is None or win_rate is None:
        return "insufficient"
    if avg_return > 0 and win_rate >= 0.52:
        return "candidate"
    if avg_return < 0 and win_rate <= 0.48:
        return "noise_candidate"
    return "mixed"


def _noise_risk(
    sample_count: int,
    unique_payload_shapes: int,
    avg_return: float | None,
    win_rate: float | None,
) -> str:
    if sample_count < 20:
        return "high_sample_risk"
    if unique_payload_shapes <= 1:
        return "shared_payload_risk"
    if avg_return is not None and abs(avg_return) < 0.001:
        return "low_edge_risk"
    if win_rate is not None and 0.48 < win_rate < 0.52:
        return "coin_flip_risk"
    return "normal"


def _recommendation(status: str) -> str:
    return {
        "candidate": "Keep out of execution, then run paper A/B validation as a candidate feature.",
        "noise_candidate": "Treat as noise until a different transform or regime filter improves it.",
        "mixed": "Segment by timeframe, symbol tier, and market regime before promotion.",
        "insufficient": "Collect more observations before judging signal quality.",
    }[status]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _weighted_average(items: list[tuple[float, int]]) -> float | None:
    total_weight = sum(weight for _value, weight in items)
    if not total_weight:
        return None
    return sum(value * weight for value, weight in items) / total_weight


def _payload_fingerprint(payload: dict[str, Any]) -> str:
    keys = sorted(key for key in payload if key != "klines")
    return ",".join(keys)
