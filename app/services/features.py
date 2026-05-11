from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from statistics import mean
from typing import Any

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    FeatureEvent as FeatureEventModel,
    FeatureLabel,
    PriceSnapshot,
    SignalSnapshot,
    utc_now,
)
from app.services.raw_payloads import payload_for_snapshot


FEATURE_HORIZONS: dict[str, timedelta] = {
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "24h": timedelta(hours=24),
}

FEATURE_LABEL_MAX_LAG: dict[str, timedelta] = {
    "30m": timedelta(minutes=45),
    "1h": timedelta(hours=1),
    "4h": timedelta(minutes=90),
    "24h": timedelta(hours=2),
}

FEATURE_REFERENCE_MAX_LAG = timedelta(minutes=45)
FEATURE_PRICE_MIN_REFERENCE_RATIO = 0.5
FEATURE_PRICE_MAX_REFERENCE_RATIO = 2.0

FEATURE_SOURCE_KEYS: tuple[str, ...] = (
    "smart_money_cost",
    "trend_price",
    "inst_vwap",
    "liq_heatmap",
    "liquidation_fuel",
    "liquidity_sweep",
    "inst_volume_profile",
    "hvn_nodes",
    "micro_poc",
    "cross_exchange_resonance",
    "imbalance",
    "trend_exhaustion",
    "fair_value_gap",
    "cascade_liquidation_zones",
    "retail_stop_loss",
    "inst_choch",
    "trend_purity",
    "liquidity_vacuum",
    "order_blocks",
    "volume_profile",
    "heatmap_data",
    "levels",
    "zones",
    "nodes",
    "events",
    "signals",
)

NESTED_FEATURE_KEYS: tuple[str, ...] = (
    "data",
    "items",
    "levels",
    "zones",
    "nodes",
    "events",
    "signals",
    "order_blocks",
    "volume_profile",
    "heatmap_data",
)

INDICATOR_SOURCE_KEYS: dict[str, tuple[str, ...]] = {
    "smart_money_cost": ("smart_money_cost",),
    "trend_price": ("order_blocks",),
    "liq_heatmap": ("heatmap_data",),
    "liquidity_sweep": ("liquidity_sweep",),
    "micro_poc": ("micro_poc",),
    "cross_exchange_resonance": ("cross_exchange_resonance",),
    "fair_value_gap": ("order_blocks",),
    "cascade_liquidation_zones": ("order_blocks",),
    "retail_stop_loss": ("order_blocks",),
    "inst_choch": ("inst_choch",),
    "trend_purity": ("trend_purity",),
    "liquidity_vacuum": ("order_blocks",),
}

CONTEXT_ONLY_INDICATORS: set[str] = {
    "hvn_nodes",
    "inst_volume_profile",
    "liquidation_fuel",
}

PRICE_OPTIONAL_INDICATORS: set[str] = {"trend_exhaustion"}
MIN_STRENGTH_BY_INDICATOR: dict[str, float] = {"trend_exhaustion": 1e-12}


@dataclass(frozen=True)
class ExtractedFeatureEvent:
    snapshot_id: str
    symbol: str
    asset_tier: str
    timeframe: str
    interval: str
    indicator: str
    event_key: str
    feature_name: str
    direction: str
    event_ts: datetime
    event_price: float | None
    strength: float
    subtype: str
    source_payload_key: str
    context: dict[str, Any]


@dataclass(frozen=True)
class FeatureEventBackfillResult:
    snapshots_scanned: int
    payloads_missing: int
    events_extracted: int
    events_inserted: int
    duplicates: int


@dataclass(frozen=True)
class FeatureLabelBackfillResult:
    events_scanned: int
    labels_labeled: int
    labels_pending: int
    labels_skipped: int
    labels_refreshed: int


@dataclass(frozen=True)
class FeatureResetResult:
    events_deleted: int
    labels_deleted: int


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


def extract_feature_events(
    snapshot: SignalSnapshot,
    payload: dict[str, Any] | None = None,
    *,
    max_events_per_snapshot: int = 200,
) -> list[ExtractedFeatureEvent]:
    raw_payload = payload if payload is not None else payload_for_snapshot(snapshot)
    if not isinstance(raw_payload, dict) or not raw_payload:
        return []
    klines = _normalize_klines(raw_payload.get("klines") or [])
    rows: list[ExtractedFeatureEvent] = []
    for source_key, source_index, item in _feature_items(snapshot.indicator, raw_payload):
        source_ts = _feature_ts(item)
        event_ts = _aware(snapshot.collected_at)
        event_price = _feature_price(item)
        if event_price is None:
            event_price = _nearest_kline_close(klines, source_ts) or _last_kline_close(klines)
        direction = _feature_direction(item)
        subtype = _feature_subtype(item)
        strength = _feature_strength(item)
        if not _is_researchable_event(snapshot.indicator, direction, event_price, strength):
            continue
        feature_name = _feature_name(snapshot.indicator, source_key)
        event_key = _event_key(
            symbol=snapshot.symbol,
            timeframe=snapshot.timeframe,
            interval=snapshot.interval,
            indicator=snapshot.indicator,
            source_payload_key=source_key,
            event_ts=event_ts,
            event_price=event_price,
            subtype=subtype,
            direction=direction,
        )
        rows.append(
            ExtractedFeatureEvent(
                snapshot_id=snapshot.id,
                symbol=snapshot.symbol,
                asset_tier=snapshot.asset_tier,
                timeframe=snapshot.timeframe,
                interval=snapshot.interval,
                indicator=snapshot.indicator,
                event_key=event_key,
                feature_name=feature_name,
                direction=direction,
                event_ts=event_ts,
                event_price=event_price,
                strength=strength,
                subtype=subtype,
                source_payload_key=source_key[:120],
                context={
                    "source_index": source_index,
                    "source_ts": source_ts.isoformat() if source_ts else None,
                    "observed_at": _aware(snapshot.collected_at).isoformat(),
                    "payload_keys": _payload_keys(item),
                    "item": _compact_item(item),
                },
            )
        )
        if max_events_per_snapshot and len(rows) >= max_events_per_snapshot:
            break
    return rows


async def backfill_feature_events(
    session: AsyncSession,
    *,
    limit: int = 500,
    indicators: list[str] | None = None,
    max_events_per_snapshot: int = 200,
    commit: bool = True,
) -> FeatureEventBackfillResult:
    query = select(SignalSnapshot).order_by(SignalSnapshot.collected_at.desc()).limit(limit)
    if indicators:
        query = query.where(SignalSnapshot.indicator.in_(indicators))
    result = await session.execute(query)
    snapshots = result.scalars().all()

    payloads_missing = 0
    extracted_count = 0
    inserted = 0
    duplicates = 0
    for snapshot in snapshots:
        raw_payload = payload_for_snapshot(snapshot)
        if not raw_payload:
            payloads_missing += 1
            continue
        extracted = extract_feature_events(
            snapshot,
            raw_payload,
            max_events_per_snapshot=max_events_per_snapshot,
        )
        extracted_count += len(extracted)
        if not extracted:
            continue
        keys = [item.event_key for item in extracted]
        existing_rows = await session.execute(
            select(FeatureEventModel.event_key).where(FeatureEventModel.event_key.in_(keys))
        )
        existing = set(existing_rows.scalars().all())
        seen: set[str] = set()
        for item in extracted:
            if item.event_key in existing or item.event_key in seen:
                duplicates += 1
                continue
            seen.add(item.event_key)
            session.add(
                FeatureEventModel(
                    snapshot_id=item.snapshot_id,
                    symbol=item.symbol,
                    asset_tier=item.asset_tier,
                    timeframe=item.timeframe,
                    interval=item.interval,
                    indicator=item.indicator,
                    event_key=item.event_key,
                    feature_name=item.feature_name,
                    direction=item.direction,
                    event_ts=item.event_ts,
                    event_price=item.event_price,
                    strength=item.strength,
                    subtype=item.subtype,
                    source_payload_key=item.source_payload_key,
                    context=item.context,
                )
            )
            inserted += 1
    if commit and inserted:
        await session.commit()
    return FeatureEventBackfillResult(
        snapshots_scanned=len(snapshots),
        payloads_missing=payloads_missing,
        events_extracted=extracted_count,
        events_inserted=inserted,
        duplicates=duplicates,
    )


async def backfill_feature_labels(
    session: AsyncSession,
    *,
    limit: int = 1000,
    horizons: list[str] | None = None,
    refresh_labeled: bool = False,
    commit: bool = True,
) -> FeatureLabelBackfillResult:
    selected_horizons = _normalize_horizons(horizons)
    earliest_ready_at = utc_now() - min(FEATURE_HORIZONS[horizon] for horizon in selected_horizons)
    done_horizon_count = (
        select(
            FeatureLabel.feature_event_id.label("feature_event_id"),
            func.count(
                func.distinct(
                    case(
                        (FeatureLabel.status.in_(["labeled", "skipped"]), FeatureLabel.horizon),
                        else_=None,
                    )
                )
            ).label("done_horizon_count"),
        )
        .where(FeatureLabel.horizon.in_(selected_horizons))
        .group_by(FeatureLabel.feature_event_id)
        .subquery()
    )
    query = (
        select(FeatureEventModel)
        .outerjoin(done_horizon_count, done_horizon_count.c.feature_event_id == FeatureEventModel.id)
        .order_by(FeatureEventModel.event_ts.desc())
        .limit(limit)
    )
    if not refresh_labeled:
        query = query.where(
            FeatureEventModel.event_ts <= earliest_ready_at,
            func.coalesce(done_horizon_count.c.done_horizon_count, 0) < len(selected_horizons),
        )
    result = await session.execute(
        query
    )
    events = result.scalars().all()
    if not events:
        return FeatureLabelBackfillResult(0, 0, 0, 0, 0)

    event_ids = [item.id for item in events]
    label_rows = await session.execute(
        select(FeatureLabel).where(
            FeatureLabel.feature_event_id.in_(event_ids),
            FeatureLabel.horizon.in_(selected_horizons),
        )
    )
    existing = {(item.feature_event_id, item.horizon): item for item in label_rows.scalars().all()}

    labeled = 0
    pending = 0
    skipped = 0
    refreshed = 0
    for event in events:
        for horizon in selected_horizons:
            label = existing.get((event.id, horizon))
            if label is not None and label.status == "labeled" and not refresh_labeled:
                continue
            label_payload = await _label_payload(session, event, horizon)
            if label is None:
                label = FeatureLabel(feature_event_id=event.id, horizon=horizon)
                session.add(label)
            else:
                refreshed += 1
            label.return_pct = label_payload["return_pct"]
            label.mfe = label_payload["mfe"]
            label.mae = label_payload["mae"]
            label.future_price = label_payload["future_price"]
            label.future_at = label_payload["future_at"]
            label.status = label_payload["status"]
            label.updated_at = utc_now()
            if label.status == "labeled":
                labeled += 1
            elif label.status == "pending":
                pending += 1
            else:
                skipped += 1
    if commit and (labeled or pending or skipped or refreshed):
        await session.commit()
    return FeatureLabelBackfillResult(
        events_scanned=len(events),
        labels_labeled=labeled,
        labels_pending=pending,
        labels_skipped=skipped,
        labels_refreshed=refreshed,
    )


async def reset_feature_research(
    session: AsyncSession,
    *,
    indicators: list[str] | None = None,
    commit: bool = True,
) -> FeatureResetResult:
    event_query = select(FeatureEventModel.id)
    if indicators:
        event_query = event_query.where(FeatureEventModel.indicator.in_(indicators))
    rows = await session.execute(event_query)
    event_ids = list(rows.scalars().all())
    if not event_ids:
        return FeatureResetResult(events_deleted=0, labels_deleted=0)
    label_result = await session.execute(
        delete(FeatureLabel).where(FeatureLabel.feature_event_id.in_(event_ids))
    )
    event_result = await session.execute(
        delete(FeatureEventModel).where(FeatureEventModel.id.in_(event_ids))
    )
    if commit:
        await session.commit()
    return FeatureResetResult(
        events_deleted=int(event_result.rowcount or 0),
        labels_deleted=int(label_result.rowcount or 0),
    )


async def refresh_feature_research(
    session: AsyncSession,
    *,
    limit: int = 500,
    indicators: list[str] | None = None,
    horizons: list[str] | None = None,
    min_samples: int = 5,
) -> dict[str, Any]:
    events = await backfill_feature_events(
        session,
        limit=limit,
        indicators=indicators,
        commit=False,
    )
    labels = await backfill_feature_labels(
        session,
        limit=limit * 10,
        horizons=horizons,
        refresh_labeled=True,
        commit=False,
    )
    await session.commit()
    effectiveness = await feature_effectiveness(
        session,
        min_samples=min_samples,
        horizon=(horizons or ["4h"])[0],
    )
    return {
        "events": events.__dict__,
        "labels": labels.__dict__,
        "effectiveness": effectiveness,
    }


async def feature_effectiveness(
    session: AsyncSession,
    *,
    min_samples: int = 5,
    horizon: str = "4h",
    limit: int = 10000,
) -> dict[str, Any]:
    _normalize_horizons([horizon])
    pair_rows = await session.execute(
        select(FeatureEventModel, FeatureLabel)
        .where(
            FeatureLabel.feature_event_id == FeatureEventModel.id,
            FeatureLabel.horizon == horizon,
            FeatureLabel.status == "labeled",
        )
        .order_by(FeatureEventModel.event_ts.desc())
        .limit(limit)
    )
    pairs = [
        (event, label)
        for event, label in pair_rows.all()
        if isinstance(label.return_pct, (int, float))
    ]
    event_count = int(
        (
            await session.execute(
                select(func.count()).select_from(FeatureEventModel)
            )
        ).scalar_one()
    )
    label_count = int(
        (
            await session.execute(
                select(func.count()).select_from(FeatureLabel).where(FeatureLabel.horizon == horizon)
            )
        ).scalar_one()
    )
    return {
        "horizon": horizon,
        "min_samples": min_samples,
        "event_count": event_count,
        "label_count": label_count,
        "labeled_count": len(pairs),
        "label_quality": _label_quality(event_count, label_count, len(pairs)),
        "policy": {
            "used_for_execution_weights": False,
            "used_for_opening_decisions": False,
            "reason": "Feature events are research evidence until promoted through paper validation.",
        },
        "features": _group_feature_effectiveness(pairs, min_samples=min_samples, key="feature"),
        "by_indicator": _group_feature_effectiveness(pairs, min_samples=min_samples, key="indicator"),
        "by_timeframe": _group_feature_effectiveness(pairs, min_samples=min_samples, key="timeframe"),
        "by_symbol": _group_feature_effectiveness(pairs, min_samples=min_samples, key="symbol"),
        "by_direction": _group_feature_effectiveness(pairs, min_samples=min_samples, key="direction"),
        "by_symbol_timeframe": _group_feature_effectiveness(pairs, min_samples=min_samples, key="symbol_timeframe"),
        "by_indicator_timeframe": _group_feature_effectiveness(pairs, min_samples=min_samples, key="indicator_timeframe"),
    }


def _safe_len(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, dict, str)):
        return len(value)
    return None


def _feature_items(indicator: str, payload: dict[str, Any]) -> list[tuple[str, int, Any]]:
    rows: list[tuple[str, int, Any]] = []
    source_keys = INDICATOR_SOURCE_KEYS.get(indicator)
    if source_keys is None:
        source_keys = (indicator,) if indicator in CONTEXT_ONLY_INDICATORS else ()
    for key in source_keys:
        if key not in payload or key == "klines":
            continue
        rows.extend(_items_from_value(key, payload[key]))
    return rows


def _items_from_value(source_key: str, value: Any) -> list[tuple[str, int, Any]]:
    rows: list[tuple[str, int, Any]] = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            if _is_feature_item(item):
                rows.append((source_key, index, item))
        return rows
    if isinstance(value, dict):
        if _is_feature_item(value):
            rows.append((source_key, 0, value))
        for nested_key in NESTED_FEATURE_KEYS:
            nested = value.get(nested_key)
            if isinstance(nested, list):
                for index, item in enumerate(nested):
                    if _is_feature_item(item):
                        rows.append((f"{source_key}.{nested_key}"[:120], index, item))
        return rows
    return rows


def _is_feature_item(item: Any) -> bool:
    if isinstance(item, dict):
        keys = set(item)
        if keys & {
            "timestamp",
            "ts",
            "time",
            "start_time",
            "origin_ts",
            "end_time",
            "price",
            "avg_price",
            "poc",
            "level",
            "low",
            "high",
            "top_price",
            "bottom_price",
            "direction",
            "type",
            "side",
            "bias",
        }:
            return True
        return False
    if isinstance(item, (list, tuple)) and len(item) <= 24:
        return any(isinstance(value, (int, float)) and value > 0 for value in item)
    return False


def _normalize_klines(raw_klines: list[Any]) -> list[dict[str, float]]:
    rows = []
    for raw in raw_klines:
        if not isinstance(raw, list) or len(raw) < 5:
            continue
        rows.append(
            {
                "ts": float(raw[0]),
                "open": float(raw[1]),
                "close": float(raw[2]),
                "low": float(raw[3]),
                "high": float(raw[4]),
            }
        )
    return rows


def _feature_ts(item: Any) -> datetime | None:
    if isinstance(item, dict):
        for key in ("timestamp", "ts", "time", "start_time", "origin_ts", "end_time", "open_time", "close_time"):
            parsed = _parse_timestamp(item.get(key))
            if parsed is not None:
                return parsed
    if isinstance(item, (list, tuple)) and item:
        return _parse_timestamp(item[0])
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw <= 0:
            return None
        seconds = raw / 1000 if raw > 10_000_000_000 else raw
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        if not normalized:
            return None
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(normalized, fmt)
                    break
                except ValueError:
                    parsed = None
            if parsed is None:
                return None
        return _aware(parsed)
    return None


def _feature_price(item: Any) -> float | None:
    if isinstance(item, dict):
        for key in (
            "price",
            "poc_price",
            "event_price",
            "level_price",
            "avg_price",
            "mid_price",
            "midpoint",
            "mid",
            "poc",
            "level",
            "value",
            "close",
            "entry_price",
            "top_price",
            "bottom_price",
        ):
            value = _positive_float(item.get(key))
            if value is not None:
                return value
        for low_key, high_key in (("low", "high"), ("bottom", "top"), ("lower", "upper")):
            low = _positive_float(item.get(low_key))
            high = _positive_float(item.get(high_key))
            if low is not None and high is not None:
                return (low + high) / 2
    if isinstance(item, (list, tuple)):
        for value in item[1:]:
            parsed = _positive_float(value)
            if parsed is not None:
                return parsed
    return None


def _feature_direction(item: Any) -> str:
    text = ""
    if isinstance(item, dict):
        values = [item.get(key) for key in ("type", "direction", "side", "bias", "signal", "trend", "label", "status")]
        text = " ".join(str(value).lower() for value in values if value is not None)
    if any(token in text for token in ("bull", "long", "buy", "demand", "support", "accumulation", "up", "positive")):
        return "long"
    if any(token in text for token in ("bear", "short", "sell", "supply", "resistance", "distribution", "down", "negative")):
        return "short"
    return "neutral"


def _feature_subtype(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("type", "subtype", "name", "label", "side", "direction"):
            value = item.get(key)
            if value not in (None, ""):
                return str(value)[:80]
    return "unknown"


def _feature_strength(item: Any) -> float:
    if isinstance(item, dict):
        for key in ("strength", "score", "confidence", "intensity", "exhaustion", "purity", "volume", "total_vol", "buy_vol", "sell_vol", "size"):
            value = _positive_float(item.get(key))
            if value is not None:
                return value
    return 0.0


def _feature_name(indicator: str, source_key: str) -> str:
    return indicator if source_key == indicator else f"{indicator}.{source_key}"[:120]


def _is_researchable_event(
    indicator: str,
    direction: str,
    event_price: float | None,
    strength: float,
) -> bool:
    if direction not in ("long", "short"):
        return False
    if event_price is None and indicator not in PRICE_OPTIONAL_INDICATORS:
        return False
    return strength >= MIN_STRENGTH_BY_INDICATOR.get(indicator, 0.0)


def _event_key(
    *,
    symbol: str,
    timeframe: str,
    interval: str,
    indicator: str,
    source_payload_key: str,
    event_ts: datetime,
    event_price: float | None,
    subtype: str,
    direction: str,
) -> str:
    raw = json.dumps(
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "interval": interval,
            "indicator": indicator,
            "source_payload_key": source_payload_key,
            "event_ts": event_ts.isoformat(),
            "event_price": round(event_price, 8) if event_price is not None else None,
            "subtype": subtype,
            "direction": direction,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _nearest_kline_close(klines: list[dict[str, float]], target_ts: datetime | None) -> float | None:
    if not klines or target_ts is None:
        return None
    target_ms = target_ts.timestamp() * 1000
    best = min(klines, key=lambda item: abs(item["ts"] - target_ms))
    return float(best["close"])


def _last_kline_close(klines: list[dict[str, float]]) -> float | None:
    return float(klines[-1]["close"]) if klines else None


def _positive_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _payload_keys(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return []
    return sorted(str(key) for key in item.keys())[:40]


def _compact_item(value: Any, *, depth: int = 0) -> Any:
    if depth >= 2:
        return str(value)[:160]
    if isinstance(value, dict):
        return {str(key): _compact_item(item, depth=depth + 1) for key, item in list(value.items())[:24]}
    if isinstance(value, (list, tuple)):
        return [_compact_item(item, depth=depth + 1) for item in list(value)[:16]]
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:160]


async def _label_payload(
    session: AsyncSession,
    event: FeatureEventModel,
    horizon: str,
) -> dict[str, Any]:
    event_ts = _aware(event.event_ts)
    if event.direction not in ("long", "short"):
        return {"status": "skipped", "return_pct": None, "mfe": None, "mae": None, "future_price": None, "future_at": None}
    base = await _price_at_or_after(session, event.symbol, event_ts)
    reference_price = _fresh_price(base, event_ts, FEATURE_REFERENCE_MAX_LAG)
    entry = reference_price
    if not entry:
        return {"status": "pending", "return_pct": None, "mfe": None, "mae": None, "future_price": None, "future_at": None}
    if event.event_price is not None and not _price_near_reference(event.event_price, reference_price):
        return {"status": "skipped", "return_pct": None, "mfe": None, "mae": None, "future_price": None, "future_at": None}
    target_at = event_ts + FEATURE_HORIZONS[horizon]
    future = await _price_at_or_after(session, event.symbol, target_at)
    if _fresh_price(future, target_at, FEATURE_LABEL_MAX_LAG[horizon]) is None:
        return {"status": "pending", "return_pct": None, "mfe": None, "mae": None, "future_price": None, "future_at": None}
    path = await _prices_between(session, event.symbol, event_ts, _aware(future.collected_at))
    returns = [_directional_return(event.direction, entry, item.price) for item in path]
    if not returns:
        returns = [_directional_return(event.direction, entry, future.price)]
    return {
        "status": "labeled",
        "return_pct": _directional_return(event.direction, entry, future.price),
        "mfe": max(returns),
        "mae": min(returns),
        "future_price": future.price,
        "future_at": future.collected_at,
    }


async def _price_at_or_after(
    session: AsyncSession,
    symbol: str,
    target_at: datetime,
) -> PriceSnapshot | None:
    rows = await session.execute(
        select(PriceSnapshot)
        .where(PriceSnapshot.symbol == symbol, PriceSnapshot.collected_at >= target_at)
        .order_by(PriceSnapshot.collected_at)
        .limit(1)
    )
    return rows.scalar_one_or_none()


def _fresh_price(
    snapshot: PriceSnapshot | None,
    target_at: datetime,
    max_lag: timedelta,
) -> float | None:
    if snapshot is None:
        return None
    if _aware(snapshot.collected_at) - target_at > max_lag:
        return None
    return snapshot.price


def _price_near_reference(entry: float, reference: float) -> bool:
    if entry <= 0 or reference <= 0:
        return False
    ratio = entry / reference
    return FEATURE_PRICE_MIN_REFERENCE_RATIO <= ratio <= FEATURE_PRICE_MAX_REFERENCE_RATIO


async def _prices_between(
    session: AsyncSession,
    symbol: str,
    start_at: datetime,
    end_at: datetime,
) -> list[PriceSnapshot]:
    rows = await session.execute(
        select(PriceSnapshot)
        .where(
            PriceSnapshot.symbol == symbol,
            PriceSnapshot.collected_at >= start_at,
            PriceSnapshot.collected_at <= end_at,
        )
        .order_by(PriceSnapshot.collected_at)
    )
    return list(rows.scalars().all())


def _directional_return(direction: str, entry: float, price: float) -> float:
    if not entry:
        return 0.0
    if direction == "short":
        return (entry - price) / entry
    return (price - entry) / entry


def _normalize_horizons(horizons: list[str] | None) -> list[str]:
    selected = horizons or list(FEATURE_HORIZONS)
    unknown = sorted(set(selected) - set(FEATURE_HORIZONS))
    if unknown:
        raise ValueError(f"Unsupported feature label horizons: {', '.join(unknown)}")
    return selected


def _group_feature_effectiveness(
    pairs: list[tuple[FeatureEventModel, FeatureLabel]],
    *,
    min_samples: int,
    key: str,
) -> list[dict[str, Any]]:
    buckets: dict[str, list[tuple[FeatureEventModel, FeatureLabel]]] = {}
    for event, label in pairs:
        buckets.setdefault(_group_key(event, key), []).append((event, label))
    rows = []
    for name, items in buckets.items():
        values = [float(label.return_pct) for _event, label in items if label.return_pct is not None]
        if len(values) < min_samples:
            continue
        wins = [value for value in values if value > 0]
        losses = [value for value in values if value < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        first = items[0][0]
        avg_return = mean(values)
        win_rate = len(wins) / len(values)
        rows.append(
            {
                "name": name,
                "indicator": first.indicator,
                "feature_name": first.feature_name,
                "subtype": first.subtype,
                "direction": first.direction,
                "timeframe": first.timeframe,
                "symbol": first.symbol,
                "sample_count": len(values),
                "win_rate": win_rate,
                "avg_return": avg_return,
                "median_return": _median(values),
                "profit_factor": gross_win / gross_loss if gross_loss else (999.0 if gross_win else None),
                "avg_mfe": _avg_label(items, "mfe"),
                "avg_mae": _avg_label(items, "mae"),
                "status": _feature_status(len(values), avg_return, win_rate, min_samples),
                "used_for_execution_weights": False,
                "used_for_opening_decisions": False,
            }
        )
    return sorted(rows, key=lambda row: (row["avg_return"], row["win_rate"], row["sample_count"]), reverse=True)


def _group_key(event: FeatureEventModel, key: str) -> str:
    if key == "indicator":
        return event.indicator
    if key == "timeframe":
        return event.timeframe
    if key == "symbol":
        return event.symbol
    if key == "direction":
        return event.direction
    if key == "symbol_timeframe":
        return f"{event.symbol}:{event.timeframe}"
    if key == "indicator_timeframe":
        return f"{event.indicator}:{event.timeframe}"
    return f"{event.feature_name}:{event.subtype}:{event.direction}"


def _label_quality(event_count: int, label_count: int, labeled_count: int) -> dict[str, Any]:
    return {
        "labeled_event_ratio": labeled_count / event_count if event_count else 0.0,
        "labeled_label_ratio": labeled_count / label_count if label_count else 0.0,
        "minimum_promotion_sample_count": 30,
        "sample_warning": "insufficient" if labeled_count < 30 else "usable_for_research",
    }


def _avg_label(items: list[tuple[FeatureEventModel, FeatureLabel]], field: str) -> float | None:
    values = [getattr(label, field) for _event, label in items]
    numeric = [float(value) for value in values if isinstance(value, (int, float))]
    return mean(numeric) if numeric else None


def _feature_status(sample_count: int, avg_return: float, win_rate: float, min_samples: int) -> str:
    if sample_count < min_samples:
        return "insufficient"
    if avg_return > 0 and win_rate >= 0.52:
        return "candidate"
    if avg_return < 0 and win_rate <= 0.48:
        return "noise_candidate"
    return "mixed"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
