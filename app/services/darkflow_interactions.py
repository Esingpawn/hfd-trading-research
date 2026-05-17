from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from statistics import mean, median
from typing import Any
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DarkflowInteraction, DarkflowZone, ExperimentRun, ShadowPaperTrade, SignalSnapshot, utc_now
from app.services.darkflow_playbooks import DEFAULT_MIN_SAMPLES, PLAYBOOKS
from app.services.darkflow_rules import official_rule_for_internal_indicator
from app.services.feature_candidates import research_query_max_limit
from app.services.raw_payloads import payload_for_snapshot
from app.services.research_lineage import core_darkflow_v2_lineage


DEFAULT_DARKFLOW_ZONE_LIMIT = 500
DEFAULT_MAX_ZONES_PER_SNAPSHOT = 120
DEFAULT_MAX_INTERACTIONS_PER_SNAPSHOT = 80
DEFAULT_MAX_HOLD_BARS = 12
DEFAULT_ZONE_WIDTH_BPS = 18.0
DEFAULT_TARGET_R = 1.8
DEFAULT_STOP_BUFFER_BPS = 12.0
DEFAULT_MIN_PROFIT_FACTOR = 1.15
DEFAULT_MIN_WIN_RATE = 0.52
DEFAULT_MIN_QUALITY_SCORE = 55.0
DEFAULT_CONFIRMATION_WINDOW_MINUTES = 90
DEFAULT_MIN_DYNAMIC_TARGET_R = 1.15
DEFAULT_MAX_DYNAMIC_TARGET_R = 6.0
DEFAULT_SHADOW_REPLAY_LIMIT = 500
DARKFLOW_INTERACTION_SCHEMA = "v2"
DARKFLOW_INTERACTION_LEGACY_SCHEMA = "legacy"
DARKFLOW_INTERACTION_STRATEGY = "darkflow_interaction_quality_v2"


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class ExtractedDarkflowZone:
    zone_key: str
    source_snapshot_id: str
    source_event_id: str | None
    symbol: str
    asset_tier: str
    timeframe: str
    interval: str
    indicator: str
    family: str
    zone_type: str
    direction: str
    lower_price: float
    upper_price: float
    mid_price: float
    strength: float
    subtype: str
    origin_ts: datetime
    detected_at: datetime
    expires_at: datetime | None
    context: dict[str, Any]


@dataclass(frozen=True)
class DetectedDarkflowInteraction:
    interaction_key: str
    zone_id: str | None
    zone_key: str
    source_snapshot_id: str
    symbol: str
    timeframe: str
    interval: str
    indicator: str
    playbook: str
    direction: str
    interaction_type: str
    event_ts: datetime
    entry_price: float | None
    stop_price: float | None
    target_price: float | None
    invalidation_price: float | None
    exit_price: float | None
    exit_ts: datetime | None
    exit_reason: str | None
    pnl_pct: float | None
    r_multiple: float | None
    mfe: float
    mae: float
    status: str
    context: dict[str, Any]


@dataclass(frozen=True)
class DarkflowZoneBackfillResult:
    snapshots_scanned: int
    payloads_missing: int
    zones_extracted: int
    zones_inserted: int
    duplicates: int


@dataclass(frozen=True)
class DarkflowInteractionBackfillResult:
    snapshots_scanned: int
    zones_extracted: int
    interactions_detected: int
    interactions_inserted: int
    duplicates: int
    skipped_without_klines: int


@dataclass(frozen=True)
class _SnapshotCandidate:
    id: str
    symbol: str
    timeframe: str
    indicator: str
    collected_at: datetime
    existing_interactions: int


def extract_darkflow_zones(
    snapshot: SignalSnapshot,
    payload: dict[str, Any] | None = None,
    *,
    max_zones_per_snapshot: int = DEFAULT_MAX_ZONES_PER_SNAPSHOT,
) -> list[ExtractedDarkflowZone]:
    raw_payload = payload if payload is not None else payload_for_snapshot(snapshot)
    if not isinstance(raw_payload, dict) or not raw_payload:
        return []
    candles = normalize_klines(raw_payload.get("klines") or [])
    reference_price = _last_close(candles)
    rows: list[ExtractedDarkflowZone] = []
    for source_key, source_index, item in _zone_items(snapshot.indicator, raw_payload):
        zone = _zone_from_item(
            snapshot,
            item,
            source_key=source_key,
            source_index=source_index,
            reference_price=reference_price,
        )
        if zone is None:
            continue
        rows.append(zone)
        if max_zones_per_snapshot and len(rows) >= max_zones_per_snapshot:
            break
    return rows


def detect_darkflow_interactions(
    zone: ExtractedDarkflowZone | DarkflowZone,
    candles: list[Candle],
    *,
    max_hold_bars: int = DEFAULT_MAX_HOLD_BARS,
    target_zones: list[ExtractedDarkflowZone | DarkflowZone] | None = None,
    trend_context: dict[str, Any] | None = None,
) -> list[DetectedDarkflowInteraction]:
    if not candles:
        return []
    zone_payload = _zone_payload(zone)
    start_index = _interaction_start_index(candles, zone_payload, max_hold_bars=max_hold_bars)
    if start_index is None:
        return []
    first_touch_index = _first_touch_index(candles, zone_payload, start_index=start_index)
    if first_touch_index is None:
        return []

    touch = candles[first_touch_index]
    interaction_type = _interaction_type(zone_payload, candles, first_touch_index)
    playbook = _playbook_for_zone(zone_payload, interaction_type)
    direction = str(zone_payload["direction"])
    entry_price = _entry_price(zone_payload, touch)
    stop_price = _stop_price(zone_payload, touch)
    target_plan = _target_plan_for_interaction(
        direction,
        entry_price,
        stop_price,
        playbook=playbook,
        source_zone=zone_payload,
        target_zones=target_zones or [],
    )
    evidence = _interaction_evidence(
        zone_payload,
        playbook=playbook,
        target_zones=target_zones or [],
        trend_context=trend_context or {},
    )
    target_price = target_plan["price"]
    hold = candles[first_touch_index : first_touch_index + max(1, int(max_hold_bars)) + 1]
    outcome = _interaction_outcome(
        direction,
        hold,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        zone=zone_payload,
    )
    runner_outcome = _runner_outcome(
        direction,
        hold,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
    )
    event_ts = touch.ts
    context = {
        "research_only": True,
        "opens_live_orders": False,
        "opens_paper_trades": False,
        "interaction_schema": DARKFLOW_INTERACTION_SCHEMA,
        "strategy_name": DARKFLOW_INTERACTION_STRATEGY,
        "zone": _compact_context(zone_payload),
        "touch_candle": _candle_payload(touch),
        "hold_bars": len(hold),
        "tutorial_rule_family": zone_payload["family"],
        "target_model": target_plan["model"],
        "target_plan": _compact_context(target_plan),
        "runner_outcome": runner_outcome,
        "evidence": _json_safe(evidence),
    }
    context["quality"] = _base_quality_profile(
        indicator=str(zone_payload["indicator"]),
        playbook=playbook,
        interaction_type=interaction_type,
        target_model=str(target_plan["model"]),
        runner_outcome=runner_outcome,
        evidence=evidence,
    )
    return [
        DetectedDarkflowInteraction(
            interaction_key=_interaction_key(zone_payload["zone_key"], interaction_type, event_ts),
            zone_id=getattr(zone, "id", None),
            zone_key=str(zone_payload["zone_key"]),
            source_snapshot_id=str(zone_payload["source_snapshot_id"]),
            symbol=str(zone_payload["symbol"]),
            timeframe=str(zone_payload["timeframe"]),
            interval=str(zone_payload["interval"]),
            indicator=str(zone_payload["indicator"]),
            playbook=playbook,
            direction=direction,
            interaction_type=interaction_type,
            event_ts=event_ts,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            invalidation_price=stop_price,
            exit_price=outcome["exit_price"],
            exit_ts=outcome["exit_ts"],
            exit_reason=outcome["exit_reason"],
            pnl_pct=outcome["pnl_pct"],
            r_multiple=outcome["r_multiple"],
            mfe=outcome["mfe"],
            mae=outcome["mae"],
            status="backtested" if outcome["exit_reason"] else "observed",
            context=context,
        )
    ]


async def backfill_darkflow_zones(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_DARKFLOW_ZONE_LIMIT,
    indicators: list[str] | None = None,
    max_zones_per_snapshot: int = DEFAULT_MAX_ZONES_PER_SNAPSHOT,
    commit: bool = True,
) -> DarkflowZoneBackfillResult:
    query = select(SignalSnapshot).order_by(SignalSnapshot.collected_at.desc()).limit(max(1, int(limit)))
    if indicators:
        query = query.where(SignalSnapshot.indicator.in_(indicators))
    rows = await session.execute(query)
    snapshots = rows.scalars().all()
    payloads_missing = 0
    zones_extracted = 0
    inserted = 0
    duplicates = 0
    seen: set[str] = set()
    for snapshot in snapshots:
        payload = payload_for_snapshot(snapshot)
        if not payload:
            payloads_missing += 1
            continue
        zones = extract_darkflow_zones(snapshot, payload, max_zones_per_snapshot=max_zones_per_snapshot)
        zones_extracted += len(zones)
        insert_rows = []
        for zone in zones:
            if zone.zone_key in seen:
                duplicates += 1
                continue
            seen.add(zone.zone_key)
            insert_rows.append(_zone_insert_row(zone))
        if insert_rows:
            rowcount = await _insert_zone_rows(session, insert_rows)
            inserted += rowcount
            duplicates += len(insert_rows) - rowcount
            if commit:
                await session.commit()
    return DarkflowZoneBackfillResult(
        snapshots_scanned=len(snapshots),
        payloads_missing=payloads_missing,
        zones_extracted=zones_extracted,
        zones_inserted=inserted,
        duplicates=duplicates,
    )


async def backfill_darkflow_interactions(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_DARKFLOW_ZONE_LIMIT,
    indicators: list[str] | None = None,
    max_zones_per_snapshot: int = DEFAULT_MAX_ZONES_PER_SNAPSHOT,
    max_interactions_per_snapshot: int = DEFAULT_MAX_INTERACTIONS_PER_SNAPSHOT,
    max_hold_bars: int = DEFAULT_MAX_HOLD_BARS,
    persist_zones: bool = True,
    commit: bool = True,
) -> DarkflowInteractionBackfillResult:
    snapshots = await _balanced_darkflow_snapshots(session, limit=max(1, int(limit)), indicators=indicators)
    trend_contexts = await _trend_contexts_for_snapshots(session, snapshots)
    zones_extracted = 0
    interactions_detected = 0
    interactions_inserted = 0
    duplicates = 0
    skipped_without_klines = 0
    seen_interactions: set[str] = set()
    for snapshot in snapshots:
        payload = payload_for_snapshot(snapshot)
        if not payload:
            continue
        candles = normalize_klines(payload.get("klines") or [])
        if not candles:
            skipped_without_klines += 1
            continue
        zones = extract_darkflow_zones(snapshot, payload, max_zones_per_snapshot=max_zones_per_snapshot)
        zones_extracted += len(zones)
        trend_context = trend_contexts.get(snapshot.symbol, {"states": []})
        if persist_zones and zones:
            await _insert_zone_rows(session, [_zone_insert_row(zone) for zone in zones])
        interaction_rows = []
        for zone in zones:
            for interaction in detect_darkflow_interactions(
                zone,
                candles,
                max_hold_bars=max_hold_bars,
                target_zones=zones,
                trend_context=trend_context,
            ):
                interactions_detected += 1
                if interaction.interaction_key in seen_interactions:
                    duplicates += 1
                    continue
                seen_interactions.add(interaction.interaction_key)
                interaction_rows.append(_interaction_insert_row(interaction))
                if max_interactions_per_snapshot and len(interaction_rows) >= max_interactions_per_snapshot:
                    break
            if max_interactions_per_snapshot and len(interaction_rows) >= max_interactions_per_snapshot:
                break
        if interaction_rows:
            rowcount = await _insert_interaction_rows(session, interaction_rows)
            interactions_inserted += rowcount
            duplicates += len(interaction_rows) - rowcount
            if commit:
                await session.commit()
    return DarkflowInteractionBackfillResult(
        snapshots_scanned=len(snapshots),
        zones_extracted=zones_extracted,
        interactions_detected=interactions_detected,
        interactions_inserted=interactions_inserted,
        duplicates=duplicates,
        skipped_without_klines=skipped_without_klines,
    )


async def _balanced_darkflow_snapshots(
    session: AsyncSession,
    *,
    limit: int,
    indicators: list[str] | None = None,
) -> list[SignalSnapshot]:
    requested_limit = max(1, int(limit))
    metadata_fetch_limit = min(max(requested_limit * 8, requested_limit), 5000, research_query_max_limit())
    query = (
        select(
            SignalSnapshot.id,
            SignalSnapshot.symbol,
            SignalSnapshot.timeframe,
            SignalSnapshot.indicator,
            SignalSnapshot.collected_at,
        )
        .order_by(SignalSnapshot.collected_at.desc(), SignalSnapshot.id.desc())
        .limit(metadata_fetch_limit)
    )
    if indicators:
        query = query.where(SignalSnapshot.indicator.in_(indicators))
    rows = await session.execute(query)
    metadata_rows = rows.all()
    if not metadata_rows:
        return []

    snapshot_ids = [str(row.id) for row in metadata_rows]
    existing_rows = await session.execute(
        select(DarkflowInteraction.source_snapshot_id, func.count(DarkflowInteraction.id))
        .where(DarkflowInteraction.source_snapshot_id.in_(snapshot_ids))
        .group_by(DarkflowInteraction.source_snapshot_id)
    )
    existing_by_snapshot = {str(snapshot_id): int(count) for snapshot_id, count in existing_rows.all()}
    candidates = [
        _SnapshotCandidate(
            id=str(row.id),
            symbol=str(row.symbol),
            timeframe=str(row.timeframe),
            indicator=str(row.indicator),
            collected_at=_aware(row.collected_at),
            existing_interactions=existing_by_snapshot.get(str(row.id), 0),
        )
        for row in metadata_rows
    ]
    selected_ids = _round_robin_snapshot_ids(candidates, requested_limit)
    snapshot_rows = await session.execute(select(SignalSnapshot).where(SignalSnapshot.id.in_(selected_ids)))
    snapshots_by_id = {snapshot.id: snapshot for snapshot in snapshot_rows.scalars().all()}
    return [snapshots_by_id[snapshot_id] for snapshot_id in selected_ids if snapshot_id in snapshots_by_id]


def _round_robin_snapshot_ids(candidates: list[_SnapshotCandidate], limit: int) -> list[str]:
    groups: dict[tuple[str, str, str], list[_SnapshotCandidate]] = {}
    for candidate in candidates:
        groups.setdefault((candidate.symbol, candidate.timeframe, candidate.indicator), []).append(candidate)
    for group in groups.values():
        group.sort(key=lambda item: (item.existing_interactions, -item.collected_at.timestamp(), item.id))

    selected: list[str] = []
    while groups and len(selected) < max(1, int(limit)):
        ordered_keys = sorted(
            groups,
            key=lambda key: (
                groups[key][0].existing_interactions,
                -groups[key][0].collected_at.timestamp(),
                key,
            ),
        )
        for key in ordered_keys:
            group = groups.get(key)
            if not group:
                continue
            selected.append(group.pop(0).id)
            if not group:
                groups.pop(key, None)
            if len(selected) >= max(1, int(limit)):
                break
    return selected


async def _trend_contexts_for_snapshots(session: AsyncSession, snapshots: list[SignalSnapshot]) -> dict[str, dict[str, Any]]:
    symbols = sorted({snapshot.symbol for snapshot in snapshots})
    if not symbols:
        return {}
    ranked = (
        select(
            SignalSnapshot.id.label("snapshot_id"),
            func.row_number()
            .over(
                partition_by=(SignalSnapshot.symbol, SignalSnapshot.timeframe),
                order_by=(SignalSnapshot.collected_at.desc(), SignalSnapshot.id.desc()),
            )
            .label("rank"),
        )
        .where(
            SignalSnapshot.symbol.in_(symbols),
            SignalSnapshot.indicator == "smart_money_cost",
            SignalSnapshot.timeframe.in_(["mid", "long"]),
        )
        .subquery()
    )
    rows = await session.execute(
        select(SignalSnapshot)
        .join(ranked, SignalSnapshot.id == ranked.c.snapshot_id)
        .where(ranked.c.rank == 1)
        .order_by(SignalSnapshot.symbol, SignalSnapshot.timeframe, SignalSnapshot.collected_at.desc(), SignalSnapshot.id.desc())
    )
    latest_by_symbol_timeframe: dict[tuple[str, str], SignalSnapshot] = {}
    for item in rows.scalars().all():
        latest_by_symbol_timeframe.setdefault((item.symbol, item.timeframe), item)

    contexts: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        states = []
        for timeframe in ("long", "mid"):
            item = latest_by_symbol_timeframe.get((symbol, timeframe))
            if item is None:
                continue
            payload = payload_for_snapshot(item)
            zones = payload.get("smart_money_cost") or payload.get("zones") or []
            bias = "unknown"
            if zones:
                bias = _trend_bias_from_item(zones[-1])
            states.append(
                {
                    "timeframe": timeframe,
                    "snapshot_id": item.id,
                    "bias": bias,
                    "collected_at": _iso(item.collected_at),
                }
            )
        contexts[symbol] = {"states": states}
    return contexts


async def darkflow_interaction_backtest(
    session: AsyncSession,
    *,
    limit: int = 5000,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_win_rate: float = DEFAULT_MIN_WIN_RATE,
    min_profit_factor: float = DEFAULT_MIN_PROFIT_FACTOR,
    min_quality_score: float = DEFAULT_MIN_QUALITY_SCORE,
    persist: bool = False,
) -> dict[str, Any]:
    requested_limit = int(limit)
    effective_limit = min(max(1, requested_limit), research_query_max_limit())
    rows = await session.execute(
        select(DarkflowInteraction)
        .where(DarkflowInteraction.status == "backtested", DarkflowInteraction.pnl_pct.isnot(None))
        .order_by(DarkflowInteraction.event_ts.desc(), DarkflowInteraction.id.desc())
        .limit(effective_limit)
    )
    interactions = list(rows.scalars().all())
    scoped_interactions = [item for item in interactions if _interaction_schema(item) == DARKFLOW_INTERACTION_SCHEMA]
    report = _interaction_report(
        scoped_interactions,
        requested_limit=requested_limit,
        limit=effective_limit,
        raw_interaction_count=len(interactions),
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_quality_score=min_quality_score,
    )
    if persist:
        report["experiment_run"] = await _persist_interaction_report(
            session,
            report=report,
            requested_limit=requested_limit,
            limit=effective_limit,
            min_samples=min_samples,
            min_win_rate=min_win_rate,
            min_profit_factor=min_profit_factor,
            min_quality_score=min_quality_score,
        )
    return report


async def latest_darkflow_interaction_backtest(session: AsyncSession) -> dict[str, Any]:
    row = await session.scalar(
        select(ExperimentRun)
        .where(ExperimentRun.name == "darkflow_interaction_backtest", ExperimentRun.status == "research")
        .order_by(ExperimentRun.created_at.desc())
        .limit(1)
    )
    if row is None:
        return {"materialized": False, "strategy_family": "darkflow_zone_interactions_v1", "playbooks": []}
    metrics = dict(row.metrics or {})
    metrics["materialized"] = True
    metrics["source_experiment_run_id"] = row.id
    metrics["generated_at"] = _iso(row.created_at)
    return metrics


async def darkflow_shadow_replay(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_SHADOW_REPLAY_LIMIT,
    min_profit_factor: float = DEFAULT_MIN_PROFIT_FACTOR,
    include_watchlist: bool = True,
) -> dict[str, Any]:
    report = await latest_darkflow_interaction_backtest(session)
    allowed = _shadow_allowed_playbooks(report, min_profit_factor=min_profit_factor, include_watchlist=include_watchlist)
    if not allowed:
        return {
            "strategy_name": DARKFLOW_INTERACTION_STRATEGY,
            "inserted": 0,
            "duplicates": 0,
            "candidate_playbooks": [],
            "skipped": [{"reason": "no_interaction_playbook_candidates"}],
            "policy": _policy(),
        }
    rows = await session.execute(
        select(DarkflowInteraction)
        .where(
            DarkflowInteraction.playbook.in_(allowed),
            DarkflowInteraction.status == "backtested",
            DarkflowInteraction.pnl_pct.isnot(None),
        )
        .order_by(DarkflowInteraction.event_ts.desc(), DarkflowInteraction.id.desc())
        .limit(max(1, int(limit)) * 3)
    )
    interactions = list(rows.scalars().all())
    signal_keys = [_shadow_signal_key(item) for item in interactions]
    existing = await _existing_shadow_keys(session, signal_keys)
    inserted = 0
    duplicates = 0
    seen: set[str] = set()
    for item in interactions:
        if inserted >= max(1, int(limit)):
            break
        signal_key = _shadow_signal_key(item)
        if signal_key in existing or signal_key in seen:
            duplicates += 1
            continue
        seen.add(signal_key)
        trade = _shadow_trade_from_interaction(item, signal_key=signal_key, source_experiment_run_id=report.get("source_experiment_run_id"))
        if trade is None:
            continue
        session.add(trade)
        inserted += 1
    if inserted:
        await session.commit()
    return {
        "strategy_name": DARKFLOW_INTERACTION_STRATEGY,
        "inserted": inserted,
        "duplicates": duplicates,
        "candidate_playbooks": allowed,
        "source_experiment_run_id": report.get("source_experiment_run_id"),
        "policy": _policy(),
    }


async def _trend_context_for_snapshot(session: AsyncSession, snapshot: SignalSnapshot) -> dict[str, Any]:
    rows = await session.execute(
        select(SignalSnapshot)
        .where(
            SignalSnapshot.symbol == snapshot.symbol,
            SignalSnapshot.indicator == "smart_money_cost",
            SignalSnapshot.timeframe.in_(["mid", "long"]),
        )
        .order_by(SignalSnapshot.timeframe, SignalSnapshot.collected_at.desc(), SignalSnapshot.id.desc())
        .limit(12)
    )
    latest_by_timeframe: dict[str, SignalSnapshot] = {}
    for item in rows.scalars().all():
        latest_by_timeframe.setdefault(item.timeframe, item)
    states = []
    for timeframe, item in sorted(latest_by_timeframe.items()):
        payload = payload_for_snapshot(item)
        zones = payload.get("smart_money_cost") or payload.get("zones") or []
        bias = "unknown"
        if zones:
            bias = _trend_bias_from_item(zones[-1])
        states.append(
            {
                "timeframe": timeframe,
                "snapshot_id": item.id,
                "bias": bias,
                "collected_at": _iso(item.collected_at),
            }
        )
    return {"states": states}


def normalize_klines(raw_klines: list[Any]) -> list[Candle]:
    rows: list[Candle] = []
    for raw in raw_klines:
        candle = _parse_candle(raw)
        if candle is not None:
            rows.append(candle)
    rows.sort(key=lambda item: item.ts)
    return rows


def _parse_candle(raw: Any) -> Candle | None:
    if isinstance(raw, dict):
        ts = _parse_timestamp(raw.get("timestamp") or raw.get("ts") or raw.get("time") or raw.get("open_time"))
        open_price = _float(raw.get("open") or raw.get("o"))
        close = _float(raw.get("close") or raw.get("c"))
        high = _float(raw.get("high") or raw.get("h"))
        low = _float(raw.get("low") or raw.get("l"))
    elif isinstance(raw, (list, tuple)) and len(raw) >= 5:
        ts = _parse_timestamp(raw[0])
        open_price = _float(raw[1])
        close = _float(raw[2])
        low = _float(raw[3])
        high = _float(raw[4])
    else:
        return None
    if ts is None or None in (open_price, close, high, low):
        return None
    high_value = max(float(high), float(low), float(open_price), float(close))
    low_value = min(float(high), float(low), float(open_price), float(close))
    return Candle(ts=ts, open=float(open_price), high=high_value, low=low_value, close=float(close))


def _zone_from_item(
    snapshot: SignalSnapshot,
    item: Any,
    *,
    source_key: str,
    source_index: int,
    reference_price: float | None,
) -> ExtractedDarkflowZone | None:
    price_bounds = _price_bounds(item)
    if price_bounds is None:
        mid_price = _feature_price(item)
        if mid_price is None:
            mid_price = reference_price
        if mid_price is None or mid_price <= 0:
            return None
        width = _zone_width(mid_price)
        lower, upper = mid_price - width, mid_price + width
    else:
        lower, upper = price_bounds
        mid_price = (lower + upper) / 2
    if lower <= 0 or upper <= 0 or lower > upper:
        return None
    if lower == upper:
        width = _zone_width(mid_price)
        lower, upper = mid_price - width, mid_price + width
    direction = _zone_direction(snapshot.indicator, item, lower=lower, upper=upper, reference_price=reference_price)
    if direction not in {"long", "short"}:
        return None
    origin_ts = _feature_ts(item) or _aware(snapshot.collected_at)
    detected_at = _aware(snapshot.collected_at)
    subtype = _subtype(item)
    family = _family(snapshot.indicator)
    zone_type = _zone_type(snapshot.indicator, source_key, subtype)
    strength = _strength(item)
    zone_key = _zone_key(
        symbol=snapshot.symbol,
        timeframe=snapshot.timeframe,
        interval=snapshot.interval,
        indicator=snapshot.indicator,
        source_key=source_key,
        source_index=source_index,
        detected_at=detected_at,
        lower=lower,
        upper=upper,
        direction=direction,
        subtype=subtype,
    )
    return ExtractedDarkflowZone(
        zone_key=zone_key,
        source_snapshot_id=snapshot.id,
        source_event_id=None,
        symbol=snapshot.symbol,
        asset_tier=snapshot.asset_tier,
        timeframe=snapshot.timeframe,
        interval=snapshot.interval,
        indicator=snapshot.indicator,
        family=family,
        zone_type=zone_type,
        direction=direction,
        lower_price=float(lower),
        upper_price=float(upper),
        mid_price=float(mid_price),
        strength=float(strength),
        subtype=subtype,
        origin_ts=origin_ts,
        detected_at=detected_at,
        expires_at=detected_at + timedelta(days=7),
        context={
            "source_payload_key": source_key,
            "source_index": source_index,
            "source_item": _compact_item(item),
            "reference_price": reference_price,
            "official_rule": _official_rule_payload(snapshot.indicator),
        },
    )


def _zone_items(indicator: str, payload: dict[str, Any]) -> list[tuple[str, int, Any]]:
    keys = _indicator_source_keys(indicator)
    rows: list[tuple[str, int, Any]] = []
    for key in keys:
        if key in payload:
            rows.extend(_items_from_value(key, payload[key]))
    return rows


def _indicator_source_keys(indicator: str) -> tuple[str, ...]:
    return {
        "smart_money_cost": ("smart_money_cost", "zones", "levels"),
        "trend_price": ("trend_price", "order_blocks", "zones", "levels"),
        "micro_poc": ("micro_poc", "poc", "levels"),
        "hvn_nodes": ("hvn_nodes", "nodes", "levels"),
        "inst_volume_profile": ("inst_volume_profile", "volume_profile", "nodes", "levels"),
        "inst_vwap": ("inst_vwap", "vwap_series", "levels"),
        "trailing_vwap": ("trailing_vwap", "vwap_series", "levels"),
        "liq_heatmap": ("liq_heatmap", "heatmap_data", "levels", "zones"),
        "liquidation_fuel": ("liquidation_fuel", "levels", "zones"),
        "liquidity_sweep": ("liquidity_sweep", "events", "levels", "zones"),
        "retail_stop_loss": ("retail_stop_loss", "stop_loss_clusters", "levels", "zones"),
        "cascade_liquidation_zones": ("cascade_liquidation_zones", "liquidation_zones", "levels", "zones"),
        "fair_value_gap": ("fair_value_gap", "fvg", "gaps", "zones"),
        "liquidity_vacuum": ("liquidity_vacuum", "vacuum_zones", "gaps", "zones"),
        "inst_choch": ("inst_choch", "levels", "events", "signals"),
        "cross_exchange_resonance": ("cross_exchange_resonance", "levels", "events", "signals"),
        "imbalance": ("imbalance", "imbalance_series", "levels", "events"),
        "power_imbalance": ("power_imbalance", "levels", "events"),
        "trend_exhaustion": ("trend_exhaustion", "levels", "events"),
        "time_exhaustion": ("time_exhaustion", "levels", "events"),
        "trend_roi": ("trend_roi", "levels", "targets"),
        "volume_exhaustion": ("volume_exhaustion", "levels", "events"),
        "max_drawdown_tolerance": ("max_drawdown_tolerance", "levels", "events"),
        "trend_saturation": ("trend_saturation", "levels", "events"),
        "trend_purity": ("trend_purity", "levels", "events"),
        "poc_shift": ("poc_shift", "levels", "events"),
        "ob_decay": ("ob_decay", "order_blocks", "levels", "zones"),
        "max_pain": ("max_pain", "levels", "zones"),
    }.get(indicator, (indicator, "levels", "zones", "events"))


def _items_from_value(source_key: str, value: Any) -> list[tuple[str, int, Any]]:
    rows: list[tuple[str, int, Any]] = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            if _is_zone_item(item):
                rows.append((source_key, index, item))
        return rows
    if isinstance(value, dict):
        if _is_zone_item(value):
            rows.append((source_key, 0, value))
        for nested_key in ("data", "items", "levels", "zones", "nodes", "events", "signals", "gaps", "targets"):
            nested = value.get(nested_key)
            if isinstance(nested, list):
                for index, item in enumerate(nested):
                    if _is_zone_item(item):
                        rows.append((f"{source_key}.{nested_key}"[:120], index, item))
        return rows
    return rows


def _is_zone_item(item: Any) -> bool:
    if isinstance(item, dict):
        return bool(set(item) & {
            "price", "avg_price", "poc", "poc_price", "level", "level_price", "mid", "mid_price", "value",
            "lower", "upper", "low", "high", "bottom", "top", "bottom_price", "top_price",
            "min_price", "max_price", "lower_price", "upper_price", "gap_low", "gap_high",
            "start_price", "end_price", "direction", "type", "side", "bias", "strength", "score", "confidence",
        })
    if isinstance(item, (list, tuple)) and len(item) <= 24:
        return any(isinstance(value, (int, float)) and value > 0 for value in item[1:])
    return False


def _price_bounds(item: Any) -> tuple[float, float] | None:
    if isinstance(item, dict):
        for low_key, high_key in (
            ("lower_price", "upper_price"), ("lower", "upper"), ("min_price", "max_price"),
            ("low", "high"), ("bottom_price", "top_price"), ("bottom", "top"),
            ("gap_low", "gap_high"), ("start_price", "end_price"),
        ):
            low = _float(item.get(low_key))
            high = _float(item.get(high_key))
            if low is not None and high is not None and low > 0 and high > 0:
                return (min(low, high), max(low, high))
    return None


def _feature_price(item: Any) -> float | None:
    if isinstance(item, dict):
        for key in (
            "price", "poc_price", "event_price", "level_price", "avg_price", "mid_price", "midpoint", "mid",
            "poc", "level", "value", "close", "entry_price", "vwap", "top_price", "bottom_price",
        ):
            value = _float(item.get(key))
            if value is not None and value > 0:
                return value
        bounds = _price_bounds(item)
        if bounds is not None:
            return (bounds[0] + bounds[1]) / 2
    if isinstance(item, (list, tuple)):
        numeric = [_float(value) for value in item[1:]]
        prices = [value for value in numeric if value is not None and value > 0]
        if prices:
            return float(prices[0])
    return None


def _zone_direction(indicator: str, item: Any, *, lower: float, upper: float, reference_price: float | None) -> str:
    text = ""
    if isinstance(item, dict):
        values = [item.get(key) for key in ("type", "direction", "side", "bias", "signal", "trend", "label", "status")]
        text = " ".join(str(value).lower() for value in values if value is not None)
    if any(token in text for token in ("bull", "long", "buy", "demand", "support", "accum", "green", "up", "positive")):
        return "long"
    if any(token in text for token in ("bear", "short", "sell", "supply", "resistance", "dist", "red", "down", "negative")):
        return "short"
    if reference_price and reference_price > 0:
        mid = (lower + upper) / 2
        tolerance = max(reference_price * 0.0005, 1e-12)
        if mid < reference_price - tolerance:
            return "long"
        if mid > reference_price + tolerance:
            return "short"
    if indicator in {"trend_exhaustion", "liquidity_sweep"}:
        return "long"
    return "neutral"


def _interaction_type(zone: dict[str, Any], candles: list[Candle], touch_index: int) -> str:
    touch = candles[touch_index]
    direction = str(zone["direction"])
    lower = float(zone["lower_price"])
    upper = float(zone["upper_price"])
    later = candles[touch_index + 1] if touch_index + 1 < len(candles) else None
    body_low = min(touch.open, touch.close)
    body_high = max(touch.open, touch.close)
    if direction == "long":
        if body_high < lower:
            return "body_break"
        if touch.low < lower and (touch.close >= lower or (later and later.close >= lower)):
            return "wick_pierce_reclaim"
        if touch.low <= upper:
            return "first_touch"
    else:
        if body_low > upper:
            return "body_break"
        if touch.high > upper and (touch.close <= upper or (later and later.close <= upper)):
            return "wick_pierce_reclaim"
        if touch.high >= lower:
            return "first_touch"
    return "first_touch"


def _first_candle_index_after(candles: list[Candle], detected_at: datetime) -> int | None:
    detected = _aware(detected_at)
    for index, candle in enumerate(candles):
        if candle.ts >= detected:
            return index
    return None


def _first_candle_index_strictly_after(candles: list[Candle], timestamp: datetime) -> int | None:
    boundary = _aware(timestamp)
    for index, candle in enumerate(candles):
        if candle.ts > boundary:
            return index
    return None


def _interaction_start_index(candles: list[Candle], zone: dict[str, Any], *, max_hold_bars: int) -> int | None:
    origin_ts = zone.get("origin_ts")
    if isinstance(origin_ts, datetime):
        index = _first_candle_index_strictly_after(candles, origin_ts)
        if index is not None:
            return index
    detected_at = zone.get("detected_at")
    if isinstance(detected_at, datetime):
        index = _first_candle_index_after(candles, detected_at)
        if index is not None:
            return index
    # Some HFD level payloads do not include per-level timestamps, while the
    # latest kline timestamp can still be a few minutes before collection time.
    # In that case, use the recent local window instead of dropping the sample.
    if candles:
        return max(0, len(candles) - max(1, int(max_hold_bars)) - 1)
    return None


def _first_touch_index(candles: list[Candle], zone: dict[str, Any], *, start_index: int) -> int | None:
    lower = float(zone["lower_price"])
    upper = float(zone["upper_price"])
    for index in range(start_index, len(candles)):
        candle = candles[index]
        if candle.high >= lower and candle.low <= upper:
            return index
    return None


def _entry_price(zone: dict[str, Any], touch: Candle) -> float:
    lower = float(zone["lower_price"])
    upper = float(zone["upper_price"])
    return min(max(touch.close, lower), upper)


def _stop_price(zone: dict[str, Any], touch: Candle) -> float:
    direction = str(zone["direction"])
    buffer = _zone_width(float(zone["mid_price"]), bps=DEFAULT_STOP_BUFFER_BPS)
    if direction == "long":
        return min(float(zone["lower_price"]) - buffer, touch.low - buffer / 2)
    return max(float(zone["upper_price"]) + buffer, touch.high + buffer / 2)


def _target_price(direction: str, entry: float, stop: float, *, target_r: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        risk = _zone_width(entry, bps=DEFAULT_STOP_BUFFER_BPS)
    return entry + risk * target_r if direction == "long" else entry - risk * target_r


def _interaction_outcome(
    direction: str,
    candles: list[Candle],
    *,
    entry_price: float,
    stop_price: float,
    target_price: float,
    zone: dict[str, Any],
) -> dict[str, Any]:
    mfe = 0.0
    mae = 0.0
    exit_price = candles[-1].close
    exit_ts = candles[-1].ts
    exit_reason = "time_exit"
    for candle in candles:
        if direction == "long":
            mfe = max(mfe, (candle.high - entry_price) / entry_price)
            mae = min(mae, (candle.low - entry_price) / entry_price)
            if candle.low <= stop_price:
                exit_price = stop_price
                exit_ts = candle.ts
                exit_reason = "stop_loss"
                break
            if candle.high >= target_price:
                exit_price = target_price
                exit_ts = candle.ts
                exit_reason = "target_hit"
                break
        else:
            mfe = max(mfe, (entry_price - candle.low) / entry_price)
            mae = min(mae, (entry_price - candle.high) / entry_price)
            if candle.high >= stop_price:
                exit_price = stop_price
                exit_ts = candle.ts
                exit_reason = "stop_loss"
                break
            if candle.low <= target_price:
                exit_price = target_price
                exit_ts = candle.ts
                exit_reason = "target_hit"
                break
    pnl_pct = (exit_price - entry_price) / entry_price if direction == "long" else (entry_price - exit_price) / entry_price
    risk_pct = abs(entry_price - stop_price) / entry_price if entry_price else None
    return {
        "exit_price": exit_price,
        "exit_ts": exit_ts,
        "exit_reason": exit_reason,
        "pnl_pct": pnl_pct,
        "r_multiple": pnl_pct / risk_pct if risk_pct else None,
        "mfe": mfe,
        "mae": mae,
    }


def _target_plan_for_interaction(
    direction: str,
    entry_price: float,
    stop_price: float,
    *,
    playbook: str,
    source_zone: dict[str, Any],
    target_zones: list[ExtractedDarkflowZone | DarkflowZone],
) -> dict[str, Any]:
    fixed_target = _target_price(direction, entry_price, stop_price, target_r=DEFAULT_TARGET_R)
    fixed_plan = {
        "price": fixed_target,
        "model": "fixed_r_fallback",
        "r_multiple": DEFAULT_TARGET_R,
        "source": "fixed_r",
        "reason": "No tutorial target zone cleared minimum R, so the research backtest used the fixed-R fallback.",
        "candidates": [],
    }
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return fixed_plan
    candidates = []
    for target_zone in target_zones:
        payload = _zone_payload(target_zone)
        if payload.get("zone_key") == source_zone.get("zone_key"):
            continue
        if not _target_zone_allowed(playbook, payload):
            continue
        price = _target_candidate_price(direction, payload)
        if price is None or not _profitable_target(direction, entry_price, price):
            continue
        r_multiple = abs(price - entry_price) / stop_distance
        if not (DEFAULT_MIN_DYNAMIC_TARGET_R <= r_multiple <= DEFAULT_MAX_DYNAMIC_TARGET_R):
            continue
        candidates.append(
            {
                "price": float(price),
                "r_multiple": r_multiple,
                "source": f"{payload.get('indicator')}.{payload.get('zone_type')}",
                "indicator": payload.get("indicator"),
                "family": payload.get("family"),
                "zone_key": payload.get("zone_key"),
                "strength": _float(payload.get("strength")) or 0.0,
                "distance_pct": abs(float(price) - entry_price) / entry_price if entry_price else None,
            }
        )
    if not candidates:
        return fixed_plan
    selected = sorted(
        candidates,
        key=lambda item: (
            item["r_multiple"],
            -_target_family_priority(str(item.get("family") or "")),
            -float(item.get("strength") or 0.0),
        ),
    )[0]
    return {
        "price": selected["price"],
        "model": "tutorial_dynamic_zone_target_v1",
        "r_multiple": selected["r_multiple"],
        "source": selected["source"],
        "reason": "Target selected from a tutorial-defined darkflow zone that is profitable and clears minimum R.",
        "selected": selected,
        "candidates": candidates[:8],
    }


def _interaction_evidence(
    zone: dict[str, Any],
    *,
    playbook: str,
    target_zones: list[ExtractedDarkflowZone | DarkflowZone],
    trend_context: dict[str, Any],
) -> dict[str, Any]:
    rule = official_rule_for_internal_indicator(str(zone.get("indicator") or ""))
    confirmation_keys = set(rule.confirmation_required if rule else ())
    blocker_keys = set(_playbook_blockers(playbook))
    confirmation_hits = []
    blocker_hits = []
    for candidate in target_zones:
        payload = _zone_payload(candidate)
        if payload.get("zone_key") == zone.get("zone_key"):
            continue
        indicator = str(payload.get("indicator") or "")
        tokens = _indicator_aliases(indicator)
        hit = {
            "indicator": indicator,
            "family": payload.get("family"),
            "zone_key": payload.get("zone_key"),
            "direction": payload.get("direction"),
        }
        if confirmation_keys & tokens:
            confirmation_hits.append(hit)
        if blocker_keys & tokens and payload.get("direction") not in {zone.get("direction"), "neutral", None}:
            blocker_hits.append(hit)
    return {
        "confirmation_hits": confirmation_hits[:6],
        "blocker_hits": blocker_hits[:6],
        "trend_alignment": _trend_alignment(str(zone.get("direction") or ""), trend_context),
    }


def _runner_outcome(
    direction: str,
    candles: list[Candle],
    *,
    entry_price: float,
    stop_price: float,
    target_price: float,
) -> dict[str, Any]:
    if not candles:
        return {"extension_available": False, "max_r": None, "locked_r": None, "exit_after_target_reason": None}
    risk = abs(entry_price - stop_price)
    if risk <= 0:
        return {"extension_available": False, "max_r": None, "locked_r": None, "exit_after_target_reason": "invalid_risk"}
    target_seen = False
    max_r = 0.0
    final_r = 0.0
    exit_after_target_reason: str | None = None
    for candle in candles:
        favorable = candle.high - entry_price if direction == "long" else entry_price - candle.low
        close_gain = candle.close - entry_price if direction == "long" else entry_price - candle.close
        max_r = max(max_r, favorable / risk)
        final_r = close_gain / risk
        if not target_seen:
            target_seen = candle.high >= target_price if direction == "long" else candle.low <= target_price
        elif direction == "long" and candle.close < target_price:
            exit_after_target_reason = "target_reclaimed_against_position"
            break
        elif direction == "short" and candle.close > target_price:
            exit_after_target_reason = "target_reclaimed_against_position"
            break
    extension_available = target_seen and max_r >= DEFAULT_TARGET_R + 0.8
    return {
        "extension_available": extension_available,
        "target_seen": target_seen,
        "max_r": round(max_r, 4),
        "locked_r": round(max(final_r, 0.0), 4),
        "exit_after_target_reason": exit_after_target_reason,
    }


def _base_quality_profile(
    *,
    indicator: str,
    playbook: str,
    interaction_type: str,
    target_model: str,
    runner_outcome: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    confirmations = []
    blockers = []
    score = 35.0
    rule = official_rule_for_internal_indicator(indicator)
    if rule is not None:
        confirmations.append("official_rule_mapped")
        score += 10.0
        if rule.single_trigger_allowed:
            confirmations.append("tutorial_allows_single_trigger")
            score += 5.0
    else:
        blockers.append("official_rule_unmapped")
    if interaction_type == "wick_pierce_reclaim":
        confirmations.append("wick_reclaim_after_sweep")
        score += 20.0
    elif interaction_type == "first_touch":
        confirmations.append("first_touch_zone_reaction")
        score += 10.0
    elif interaction_type == "body_break":
        blockers.append("body_break_invalidation")
        score -= 25.0
    if target_model == "tutorial_dynamic_zone_target_v1":
        confirmations.append("dynamic_darkflow_target")
        score += 15.0
    else:
        blockers.append("fixed_r_target_fallback")
    if runner_outcome.get("extension_available"):
        confirmations.append("trend_extension_available")
        score += 8.0
    trend_alignment = evidence.get("trend_alignment") or {}
    if trend_alignment.get("aligned") is True:
        confirmations.append("parent_trend_aligned")
        score += 12.0
    elif trend_alignment.get("aligned") is False:
        blockers.append("parent_trend_conflict")
        score -= 18.0
    confirmation_hits = evidence.get("confirmation_hits") or []
    blocker_hits = evidence.get("blocker_hits") or []
    if confirmation_hits:
        confirmations.append("confirmation_indicators_nearby")
        score += min(10.0, len(confirmation_hits) * 4.0)
    if blocker_hits:
        blockers.append("blocker_indicators_nearby")
        score -= min(15.0, len(blocker_hits) * 5.0)
    if playbook == "exhaustion_exit_filter":
        blockers.append("exit_filter_not_opening_playbook")
        score -= 10.0
    return {
        "score": round(max(0.0, min(100.0, score)), 3),
        "confirmations": confirmations,
        "blockers": blockers,
        "grade": _quality_grade(score),
    }


def _interaction_report(
    interactions: list[DarkflowInteraction],
    *,
    requested_limit: int,
    limit: int,
    raw_interaction_count: int,
    min_samples: int,
    min_win_rate: float,
    min_profit_factor: float,
    min_quality_score: float,
) -> dict[str, Any]:
    buckets: dict[str, list[DarkflowInteraction]] = {}
    for item in interactions:
        buckets.setdefault(item.playbook, []).append(item)
    rows = []
    for playbook, items in buckets.items():
        stats = _stats(items)
        quality_items = _quality_filtered(items, min_quality_score=min_quality_score)
        quality_stats = _stats(quality_items)
        readiness = _readiness(
            quality_stats,
            min_samples=min_samples,
            min_win_rate=min_win_rate,
            min_profit_factor=min_profit_factor,
            quality_sample_count=len(quality_items),
        )
        rows.append(
            {
                "playbook": playbook,
                "display_name": _playbook_display_name(playbook),
                "sample_count": stats["trade_count"],
                "quality_sample_count": quality_stats["trade_count"],
                "stats": stats,
                "quality_stats": quality_stats,
                "quality": _quality_summary(items, min_quality_score=min_quality_score),
                "top_interaction_types": _top_interaction_types(items),
                "top_indicators": _top_indicators(items),
                "latest_interactions": _latest_interactions(items),
                "readiness": readiness,
            }
        )
    all_stats = _stats(interactions)
    quality_interactions = _quality_filtered(interactions, min_quality_score=min_quality_score)
    quality_stats = _stats(quality_interactions)
    return {
        "strategy_family": "darkflow_zone_interactions_v1",
        "interaction_schema": DARKFLOW_INTERACTION_SCHEMA,
        "requested_limit": requested_limit,
        "limit": limit,
        "limit_capped": requested_limit != limit,
        "raw_interaction_count": raw_interaction_count,
        "interaction_count": len(interactions),
        "backtested_count": all_stats["trade_count"],
        "quality_interaction_count": quality_stats["trade_count"],
        "candidate_playbook_count": sum(1 for row in rows if row["readiness"]["status"] == "candidate"),
        "watchlist_playbook_count": sum(1 for row in rows if row["readiness"]["status"] == "watchlist"),
        "stats": all_stats,
        "quality_stats": quality_stats,
        "quality": _quality_summary(interactions, min_quality_score=min_quality_score),
        "thresholds": {
            "min_samples": int(min_samples),
            "min_win_rate": float(min_win_rate),
            "min_profit_factor": float(min_profit_factor),
            "min_quality_score": float(min_quality_score),
        },
        "policy": _policy(),
        "playbooks": sorted(
            rows,
            key=lambda row: (
                row["readiness"]["status"] == "candidate",
                row["readiness"]["status"] == "watchlist",
                row["quality_sample_count"],
                row["quality_stats"].get("profit_factor") or 0.0,
            ),
            reverse=True,
        ),
        "implementation_gap": {
            "remaining_before_real_paper_integration": [
                "Add multi-timeframe parent trend agreement.",
                "Run isolated shadow-paper forward sample before changing paper-scan openings.",
                "Validate dynamic target and runner exits on fresh forward samples, not only historical replay.",
            ]
        },
    }


def _stats(items: list[DarkflowInteraction]) -> dict[str, Any]:
    ordered = sorted(items, key=lambda item: _aware(item.event_ts))
    returns = [_float(item.pnl_pct) for item in ordered]
    values = [value for value in returns if value is not None]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trade_count": len(values),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": len(wins) / len(values) if values else None,
        "avg_return": mean(values) if values else None,
        "median_return": median(values) if values else None,
        "profit_factor": gross_win / gross_loss if gross_loss else (999.0 if gross_win else None),
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "avg_r_multiple": _mean_or_none([float(item.r_multiple) for item in ordered if isinstance(item.r_multiple, (int, float))]),
        "avg_mfe": mean([float(item.mfe) for item in ordered]) if ordered else None,
        "avg_mae": mean([float(item.mae) for item in ordered]) if ordered else None,
        "max_drawdown": _max_drawdown(values),
        "first_event_ts": _iso(ordered[0].event_ts) if ordered else None,
        "latest_event_ts": _iso(ordered[-1].event_ts) if ordered else None,
    }


def _readiness(
    stats: dict[str, Any],
    *,
    min_samples: int,
    min_win_rate: float,
    min_profit_factor: float,
    quality_sample_count: int | None = None,
) -> dict[str, Any]:
    blockers = []
    if stats["trade_count"] < min_samples:
        blockers.append("sample_count_below_minimum")
    if quality_sample_count is not None and quality_sample_count <= 0:
        blockers.append("no_quality_samples")
    if stats["win_rate"] is None or stats["win_rate"] < min_win_rate:
        blockers.append("win_rate_below_minimum")
    if stats["profit_factor"] is None or stats["profit_factor"] < min_profit_factor:
        blockers.append("profit_factor_below_minimum")
    if not blockers:
        status = "candidate"
    elif set(blockers).issubset({"sample_count_below_minimum"}) and (stats["profit_factor"] or 0) >= min_profit_factor:
        status = "watchlist"
    else:
        status = "rejected"
    return {"status": status, "blockers": blockers, "used_for_opening_decisions": False}


async def _persist_interaction_report(
    session: AsyncSession,
    *,
    report: dict[str, Any],
    requested_limit: int,
    limit: int,
    min_samples: int,
    min_win_rate: float,
    min_profit_factor: float,
    min_quality_score: float,
) -> dict[str, str]:
    item = ExperimentRun(
        name="darkflow_interaction_backtest",
        status="research",
        scope={"requested_limit": requested_limit, "limit": limit, "interaction_count": report["interaction_count"]},
        params={
            "min_samples": min_samples,
            "min_win_rate": min_win_rate,
            "min_profit_factor": min_profit_factor,
            "min_quality_score": min_quality_score,
        },
        metrics={key: value for key, value in report.items() if key != "experiment_run"},
        notes="Darkflow zone-interaction backtest from tutorial semantics. Research-only; does not open paper/live orders.",
    )
    session.add(item)
    await session.commit()
    return {"id": item.id, "name": item.name, "status": item.status}


def _policy() -> dict[str, Any]:
    return {
        "research_only": True,
        "opens_live_orders": False,
        "opens_paper_trades": False,
        "changes_strategy_weights": False,
        "used_for_opening_decisions": False,
        "lineage": core_darkflow_v2_lineage(),
        "strategy_boundary": "eligible for isolated shadow-paper only after interaction backtest evidence",
    }


def _zone_payload(zone: ExtractedDarkflowZone | DarkflowZone) -> dict[str, Any]:
    if isinstance(zone, ExtractedDarkflowZone):
        payload = asdict(zone)
    else:
        payload = {
            "id": zone.id,
            "zone_key": zone.zone_key,
            "source_snapshot_id": zone.source_snapshot_id,
            "source_event_id": zone.source_event_id,
            "symbol": zone.symbol,
            "asset_tier": zone.asset_tier,
            "timeframe": zone.timeframe,
            "interval": zone.interval,
            "indicator": zone.indicator,
            "family": zone.family,
            "zone_type": zone.zone_type,
            "direction": zone.direction,
            "lower_price": zone.lower_price,
            "upper_price": zone.upper_price,
            "mid_price": zone.mid_price,
            "strength": zone.strength,
            "subtype": zone.subtype,
            "origin_ts": zone.origin_ts,
            "detected_at": zone.detected_at,
            "expires_at": zone.expires_at,
            "context": zone.context,
        }
    return payload


def _zone_insert_row(zone: ExtractedDarkflowZone) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "zone_key": zone.zone_key,
        "source_snapshot_id": zone.source_snapshot_id,
        "source_event_id": zone.source_event_id,
        "symbol": zone.symbol,
        "asset_tier": zone.asset_tier,
        "timeframe": zone.timeframe,
        "interval": zone.interval,
        "indicator": zone.indicator,
        "family": zone.family,
        "zone_type": zone.zone_type,
        "direction": zone.direction,
        "lower_price": zone.lower_price,
        "upper_price": zone.upper_price,
        "mid_price": zone.mid_price,
        "strength": zone.strength,
        "subtype": zone.subtype,
        "origin_ts": zone.origin_ts,
        "detected_at": zone.detected_at,
        "expires_at": zone.expires_at,
        "touches": 0,
        "status": "active",
        "context": zone.context,
            "created_at": utc_now(),
    }


def _interaction_insert_row(item: DetectedDarkflowInteraction) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "interaction_key": item.interaction_key,
        "zone_id": item.zone_id,
        "zone_key": item.zone_key,
        "source_snapshot_id": item.source_snapshot_id,
        "symbol": item.symbol,
        "timeframe": item.timeframe,
        "interval": item.interval,
        "indicator": item.indicator,
        "playbook": item.playbook,
        "direction": item.direction,
        "interaction_type": item.interaction_type,
        "event_ts": item.event_ts,
        "entry_price": item.entry_price,
        "stop_price": item.stop_price,
        "target_price": item.target_price,
        "invalidation_price": item.invalidation_price,
        "exit_price": item.exit_price,
        "exit_ts": item.exit_ts,
        "exit_reason": item.exit_reason,
        "pnl_pct": item.pnl_pct,
        "r_multiple": item.r_multiple,
        "mfe": item.mfe,
        "mae": item.mae,
        "status": item.status,
        "context": item.context,
        "created_at": utc_now(),
    }


async def _insert_zone_rows(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        stmt = postgresql_insert(DarkflowZone).values(rows).on_conflict_do_nothing(index_elements=["zone_key"]).returning(DarkflowZone.id)
        result = await session.execute(stmt)
        return len(result.scalars().all())
    if dialect_name == "sqlite":
        stmt = sqlite_insert(DarkflowZone).values(rows).on_conflict_do_nothing(index_elements=["zone_key"])
        result = await session.execute(stmt)
        return int(result.rowcount or 0)
    existing_rows = await session.execute(select(DarkflowZone.zone_key).where(DarkflowZone.zone_key.in_([row["zone_key"] for row in rows])))
    existing = set(existing_rows.scalars().all())
    pending = [row for row in rows if row["zone_key"] not in existing]
    if pending:
        await session.execute(DarkflowZone.__table__.insert(), pending)
    return len(pending)


async def _insert_interaction_rows(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        stmt = postgresql_insert(DarkflowInteraction).values(rows).on_conflict_do_nothing(index_elements=["interaction_key"]).returning(DarkflowInteraction.id)
        result = await session.execute(stmt)
        return len(result.scalars().all())
    if dialect_name == "sqlite":
        stmt = sqlite_insert(DarkflowInteraction).values(rows).on_conflict_do_nothing(index_elements=["interaction_key"])
        result = await session.execute(stmt)
        return int(result.rowcount or 0)
    existing_rows = await session.execute(
        select(DarkflowInteraction.interaction_key).where(DarkflowInteraction.interaction_key.in_([row["interaction_key"] for row in rows]))
    )
    existing = set(existing_rows.scalars().all())
    pending = [row for row in rows if row["interaction_key"] not in existing]
    if pending:
        await session.execute(DarkflowInteraction.__table__.insert(), pending)
    return len(pending)


def _shadow_trade_from_interaction(
    item: DarkflowInteraction,
    *,
    signal_key: str,
    source_experiment_run_id: Any,
) -> ShadowPaperTrade | None:
    if not all(isinstance(value, (int, float)) for value in (item.entry_price, item.stop_price, item.target_price, item.pnl_pct)):
        return None
    return ShadowPaperTrade(
        strategy_name=DARKFLOW_INTERACTION_STRATEGY,
        candidate_type="darkflow_interaction_playbook",
        candidate_key=f"{item.playbook}:{item.symbol}:{item.timeframe}:{item.direction}",
        signal_key=signal_key,
        source_experiment_run_id=str(source_experiment_run_id) if source_experiment_run_id else None,
        symbol=item.symbol,
        timeframe=item.timeframe,
        direction=item.direction,
        entry_price=float(item.entry_price),
        stop_loss=float(item.stop_price),
        take_profit=float(item.target_price),
        position_size=1.0,
        status="closed" if item.exit_price is not None else "open",
        exit_price=float(item.exit_price) if item.exit_price is not None else None,
        exit_reason=item.exit_reason,
        pnl=float(item.pnl_pct),
        r_multiple=float(item.r_multiple) if item.r_multiple is not None else None,
        mfe=float(item.mfe),
        mae=float(item.mae),
        context={
            "historical_replay": True,
            "darkflow_interaction_id": item.id,
            "interaction_schema": _interaction_schema(item),
            "playbook": item.playbook,
            "interaction_type": item.interaction_type,
            "quality": (item.context or {}).get("quality"),
            "target_plan": (item.context or {}).get("target_plan"),
            "runner_outcome": (item.context or {}).get("runner_outcome"),
            "research_only": True,
            "opens_paper_trades": False,
            "opens_live_orders": False,
        },
        opened_at=item.event_ts,
        closed_at=item.exit_ts,
    )


def _shadow_allowed_playbooks(report: dict[str, Any], *, min_profit_factor: float, include_watchlist: bool) -> list[str]:
    allowed = []
    for row in report.get("playbooks") or []:
        readiness = row.get("readiness") or {}
        status = readiness.get("status")
        pf = ((row.get("quality_stats") or row.get("stats") or {}).get("profit_factor"))
        if status == "candidate" or (include_watchlist and status == "watchlist" and isinstance(pf, (int, float)) and pf >= min_profit_factor):
            allowed.append(str(row.get("playbook") or ""))
    return [item for item in allowed if item]


async def _existing_shadow_keys(session: AsyncSession, signal_keys: list[str]) -> set[str]:
    if not signal_keys:
        return set()
    rows = await session.execute(select(ShadowPaperTrade.signal_key).where(ShadowPaperTrade.signal_key.in_(signal_keys)))
    return set(rows.scalars().all())


def _shadow_signal_key(item: DarkflowInteraction) -> str:
    return f"darkflow-interaction:{DARKFLOW_INTERACTION_SCHEMA}:{item.interaction_key}"


def _top_interaction_types(items: list[DarkflowInteraction]) -> list[dict[str, Any]]:
    buckets: dict[str, list[DarkflowInteraction]] = {}
    for item in items:
        buckets.setdefault(item.interaction_type, []).append(item)
    return sorted(({"interaction_type": key, **_stats(value)} for key, value in buckets.items()), key=lambda row: row["trade_count"], reverse=True)[:8]


def _top_indicators(items: list[DarkflowInteraction]) -> list[dict[str, Any]]:
    buckets: dict[str, list[DarkflowInteraction]] = {}
    for item in items:
        buckets.setdefault(item.indicator, []).append(item)
    return sorted(({"indicator": key, **_stats(value)} for key, value in buckets.items()), key=lambda row: row["trade_count"], reverse=True)[:8]


def _quality_filtered(items: list[DarkflowInteraction], *, min_quality_score: float) -> list[DarkflowInteraction]:
    return [item for item in items if _quality_score(item) >= min_quality_score and not _hard_quality_blocked(item)]


def _quality_summary(items: list[DarkflowInteraction], *, min_quality_score: float) -> dict[str, Any]:
    scores = [_quality_score(item) for item in items]
    quality_items = _quality_filtered(items, min_quality_score=min_quality_score)
    confirmations: dict[str, int] = {}
    blockers: dict[str, int] = {}
    target_models: dict[str, int] = {}
    runner_extension_count = 0
    parent_aligned_count = 0
    parent_conflict_count = 0
    for item in items:
        context = item.context or {}
        quality = context.get("quality") or {}
        for key in quality.get("confirmations") or []:
            confirmations[str(key)] = confirmations.get(str(key), 0) + 1
        for key in quality.get("blockers") or []:
            blockers[str(key)] = blockers.get(str(key), 0) + 1
        target_model = str(context.get("target_model") or ((context.get("target_plan") or {}).get("model") or "unknown"))
        target_models[target_model] = target_models.get(target_model, 0) + 1
        if (context.get("runner_outcome") or {}).get("extension_available"):
            runner_extension_count += 1
        alignment = ((context.get("evidence") or {}).get("trend_alignment") or {}).get("aligned")
        if alignment is True:
            parent_aligned_count += 1
        elif alignment is False:
            parent_conflict_count += 1
    return {
        "min_quality_score": float(min_quality_score),
        "quality_sample_count": len(quality_items),
        "quality_ratio": len(quality_items) / len(items) if items else 0.0,
        "avg_quality_score": mean(scores) if scores else None,
        "median_quality_score": median(scores) if scores else None,
        "top_confirmations": _top_counts(confirmations),
        "top_blockers": _top_counts(blockers),
        "target_models": _top_counts(target_models),
        "runner_extension_count": runner_extension_count,
        "runner_extension_rate": runner_extension_count / len(items) if items else 0.0,
        "parent_aligned_count": parent_aligned_count,
        "parent_conflict_count": parent_conflict_count,
    }


def _quality_score(item: DarkflowInteraction) -> float:
    context = item.context or {}
    if not context.get("quality"):
        return _legacy_quality_score(item)
    raw = (context.get("quality") or {}).get("score")
    parsed = _float(raw)
    return float(parsed or 0.0)


def _interaction_schema(item: DarkflowInteraction) -> str:
    context = item.context or {}
    raw = (item.context or {}).get("interaction_schema")
    if raw:
        return str(raw)
    if context.get("quality") and context.get("evidence") and context.get("target_plan"):
        return DARKFLOW_INTERACTION_SCHEMA
    return DARKFLOW_INTERACTION_LEGACY_SCHEMA


def _hard_quality_blocked(item: DarkflowInteraction) -> bool:
    blockers = set(((item.context or {}).get("quality") or {}).get("blockers") or [])
    return bool(
        blockers
        & {"body_break_invalidation", "official_rule_unmapped", "exit_filter_not_opening_playbook", "parent_trend_conflict"}
    )


def _legacy_quality_score(item: DarkflowInteraction) -> float:
    score = 35.0
    if official_rule_for_internal_indicator(item.indicator) is not None:
        score += 10.0
    if item.interaction_type == "wick_pierce_reclaim":
        score += 20.0
    elif item.interaction_type == "first_touch":
        score += 10.0
    elif item.interaction_type == "body_break":
        score -= 25.0
    if item.exit_reason == "target_hit":
        score += 10.0
    if isinstance(item.r_multiple, (int, float)) and item.r_multiple >= DEFAULT_TARGET_R:
        score += 5.0
    return round(max(0.0, min(100.0, score)), 3)


def _top_counts(counts: dict[str, int], *, limit: int = 8) -> list[dict[str, Any]]:
    return [
        {"key": key, "count": value}
        for key, value in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
    ]


def _latest_interactions(items: list[DarkflowInteraction]) -> list[dict[str, Any]]:
    latest = sorted(items, key=lambda item: _aware(item.event_ts), reverse=True)[:8]
    return [
        {
            "id": item.id,
            "symbol": item.symbol,
            "timeframe": item.timeframe,
            "indicator": item.indicator,
            "direction": item.direction,
            "interaction_type": item.interaction_type,
            "event_ts": _iso(item.event_ts),
            "pnl_pct": item.pnl_pct,
            "r_multiple": item.r_multiple,
            "exit_reason": item.exit_reason,
            "quality_score": _quality_score(item),
            "target_model": (item.context or {}).get("target_model"),
        }
        for item in latest
    ]


def _playbook_for_zone(zone: dict[str, Any], interaction_type: str) -> str:
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
    for playbook in PLAYBOOKS:
        if indicator in playbook.entry_indicators:
            return playbook.key
    return "darkflow_zone_reaction"


def _playbook_display_name(key: str) -> str:
    for playbook in PLAYBOOKS:
        if playbook.key == key:
            return playbook.display_name
    return key


def _playbook_blockers(key: str) -> tuple[str, ...]:
    for playbook in PLAYBOOKS:
        if playbook.key == key:
            return playbook.blocker_indicators
    return ()


def _indicator_aliases(indicator: str) -> set[str]:
    aliases = {indicator}
    rule = official_rule_for_internal_indicator(indicator)
    if rule is not None:
        aliases.add(rule.official_key)
        aliases.update(rule.internal_keys)
    return aliases


def _trend_alignment(direction: str, trend_context: dict[str, Any]) -> dict[str, Any]:
    states = [item for item in trend_context.get("states") or [] if isinstance(item, dict)]
    if not states:
        return {"aligned": None, "reason": "missing_parent_trend", "states": []}
    directional = [item for item in states if item.get("bias") in {"long", "short"}]
    if not directional:
        return {"aligned": None, "reason": "parent_trend_neutral", "states": states}
    aligned = [item for item in directional if item.get("bias") == direction]
    conflicts = [item for item in directional if item.get("bias") != direction]
    return {
        "aligned": bool(aligned) and not conflicts,
        "aligned_count": len(aligned),
        "conflict_count": len(conflicts),
        "states": states,
    }


def _trend_bias_from_item(item: Any) -> str:
    if not isinstance(item, dict):
        return "unknown"
    text = " ".join(
        str(item.get(key) or "").lower()
        for key in ("type", "direction", "side", "bias", "trend", "status", "label")
    )
    if any(token in text for token in ("accum", "bull", "long", "green", "support", "up")):
        return "long"
    if any(token in text for token in ("dist", "bear", "short", "red", "resistance", "down")):
        return "short"
    return "unknown"


def _target_zone_allowed(playbook: str, zone: dict[str, Any]) -> bool:
    indicator = str(zone.get("indicator") or "")
    family = str(zone.get("family") or "")
    if family in {"liquidity", "vacuum", "volume_profile", "cost_structure"}:
        return True
    for item in PLAYBOOKS:
        if item.key == playbook and indicator in item.target_indicators:
            return True
    return False


def _target_candidate_price(direction: str, zone: dict[str, Any]) -> float | None:
    lower = _float(zone.get("lower_price"))
    upper = _float(zone.get("upper_price"))
    mid = _float(zone.get("mid_price"))
    if lower is None or upper is None:
        return mid
    if direction == "long":
        return lower if lower > 0 else mid
    return upper if upper > 0 else mid


def _target_family_priority(family: str) -> float:
    return {
        "liquidity": 4.0,
        "vacuum": 3.5,
        "volume_profile": 3.0,
        "cost_structure": 2.5,
    }.get(family, 1.0)


def _profitable_target(direction: str, entry_price: float, target_price: float) -> bool:
    if direction == "long":
        return target_price > entry_price
    if direction == "short":
        return target_price < entry_price
    return False


def _quality_grade(score: float) -> str:
    if score >= 75:
        return "A"
    if score >= 60:
        return "B"
    if score >= 45:
        return "C"
    return "D"


def _family(indicator: str) -> str:
    rule = official_rule_for_internal_indicator(indicator)
    if rule is not None:
        return rule.family
    if indicator in {"hvn_nodes", "inst_volume_profile"}:
        return "volume_profile"
    return "unknown"


def _zone_type(indicator: str, source_key: str, subtype: str) -> str:
    family = _family(indicator)
    if family == "liquidity":
        return "liquidity_zone"
    if family in {"cost_structure", "volume_profile"}:
        return "cost_zone"
    if family == "vacuum":
        return "vacuum_zone"
    if family == "lifecycle":
        return "lifecycle_line"
    if family in {"structure_break", "orderflow"}:
        return "confirmation_level"
    return (subtype or source_key or indicator)[:80]


def _zone_width(price: float, *, bps: float = DEFAULT_ZONE_WIDTH_BPS) -> float:
    return max(abs(price) * bps / 10000.0, 1e-9)


def _zone_key(**payload: Any) -> str:
    raw = json.dumps(_json_safe(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _interaction_key(zone_key: str, interaction_type: str, event_ts: datetime) -> str:
    raw = f"{DARKFLOW_INTERACTION_SCHEMA}:{zone_key}:{interaction_type}:{_aware(event_ts).isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
            return _aware(datetime.fromisoformat(normalized))
        except ValueError:
            return None
    return None


def _strength(item: Any) -> float:
    if isinstance(item, dict):
        for key in ("strength", "score", "confidence", "intensity", "volume", "total", "fuel", "size", "weight"):
            value = _float(item.get(key))
            if value is not None:
                return abs(value)
    return 0.0


def _subtype(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("type", "subtype", "name", "label", "side", "direction"):
            value = item.get(key)
            if value not in (None, ""):
                return str(value)[:80]
    return "unknown"


def _last_close(candles: list[Candle]) -> float | None:
    return float(candles[-1].close) if candles else None


def _float(value: Any) -> float | None:
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


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value is not None else None


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    return max_dd


def _mean_or_none(values: list[float]) -> float | None:
    return mean(values) if values else None


def _official_rule_payload(indicator: str) -> dict[str, Any] | None:
    rule = official_rule_for_internal_indicator(indicator)
    if rule is None:
        return None
    return {"official_key": rule.official_key, "family": rule.family, "primary_roles": list(rule.primary_roles)}


def _compact_context(value: Any) -> Any:
    return _compact_item(_json_safe(value))


def _compact_item(value: Any, *, depth: int = 0) -> Any:
    if depth >= 2:
        return str(value)[:180]
    if isinstance(value, dict):
        return {str(key): _compact_item(item, depth=depth + 1) for key, item in list(value.items())[:28]}
    if isinstance(value, (list, tuple)):
        return [_compact_item(item, depth=depth + 1) for item in list(value)[:18]]
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)[:180]


def _candle_payload(candle: Candle) -> dict[str, Any]:
    return {"ts": candle.ts.isoformat(), "open": candle.open, "high": candle.high, "low": candle.low, "close": candle.close}


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
