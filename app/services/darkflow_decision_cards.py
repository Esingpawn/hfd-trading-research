from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DarkflowInteraction, TradeCandidate, utc_now
from app.services.darkflow_interactions import DARKFLOW_INTERACTION_SCHEMA
from app.services.darkflow_playbooks import PLAYBOOKS
from app.services.research_lineage import core_darkflow_v2_lineage


DEFAULT_DECISION_CARD_LIMIT = 20
DEFAULT_MIN_DECISION_CARD_QUALITY = 55.0
DEFAULT_MIN_RR_RATIO = 1.5
DEFAULT_TRADE_CANDIDATE_LIMIT = 100
FROZEN_ENTRY_PLAN_TYPE = "frozen_darkflow_v2_entry_plan"
DEFAULT_ENTRY_PLAN_VALID_BARS = 12
DEFAULT_ENTRY_PLAN_TOLERANCE_PCT = 0.006
DEFAULT_ENTRY_PLAN_DRIFT_LIMIT_PCT = 0.003
PROMOTION_BLOCKER_ANTI_REPAINT_MISSING = "anti_repaint_audit_missing"
PROMOTION_BLOCKER_ANTI_REPAINT_FAILED = "anti_repaint_audit_failed"
PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING = "isolated_v2_shadow_forward_sample_missing"
PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING = "isolated_v2_shadow_forward_sample_collecting"
PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED = "isolated_v2_shadow_forward_sample_failed"
PROMOTION_BLOCKER_PERSISTENT_TABLE_MISSING = "persistent_trade_candidate_table_missing"
_HARD_QUALITY_BLOCKERS = {
    "body_break_invalidation",
    "official_rule_unmapped",
    "exit_filter_not_opening_playbook",
    "parent_trend_conflict",
}


async def latest_darkflow_decision_cards(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_DECISION_CARD_LIMIT,
    min_quality_score: float = DEFAULT_MIN_DECISION_CARD_QUALITY,
    min_rr_ratio: float = DEFAULT_MIN_RR_RATIO,
) -> dict[str, Any]:
    requested_limit = max(1, int(limit))
    fetch_limit = min(max(requested_limit * 8, requested_limit), 1000)
    rows = await session.scalars(
        select(DarkflowInteraction)
        .where(
            DarkflowInteraction.status == "backtested",
            DarkflowInteraction.entry_price.isnot(None),
            DarkflowInteraction.stop_price.isnot(None),
            DarkflowInteraction.target_price.isnot(None),
        )
        .order_by(DarkflowInteraction.event_ts.desc(), DarkflowInteraction.id.desc())
        .limit(fetch_limit)
    )
    scanned = 0
    cards: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in rows.all():
        scanned += 1
        card = _decision_card(item, min_quality_score=min_quality_score, min_rr_ratio=min_rr_ratio)
        if card is None:
            skipped.append({"id": item.id, "reason": "not_core_darkflow_v2_or_invalid"})
            continue
        cards.append(card)
        if len(cards) >= requested_limit:
            break
    return {
        "strategy_family": "darkflow_trade_decision_cards_v1",
        "interaction_schema": DARKFLOW_INTERACTION_SCHEMA,
        "requested_limit": requested_limit,
        "scanned_interactions": scanned,
        "card_count": len(cards),
        "cards": cards,
        "skipped": skipped[:20],
        "thresholds": {
            "min_quality_score": float(min_quality_score),
            "min_rr_ratio": float(min_rr_ratio),
        },
        "policy": _policy(),
    }


async def materialize_darkflow_trade_candidates(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_TRADE_CANDIDATE_LIMIT,
    min_quality_score: float = DEFAULT_MIN_DECISION_CARD_QUALITY,
    min_rr_ratio: float = DEFAULT_MIN_RR_RATIO,
) -> dict[str, Any]:
    report = await latest_darkflow_decision_cards(
        session,
        limit=limit,
        min_quality_score=min_quality_score,
        min_rr_ratio=min_rr_ratio,
    )
    inserted = 0
    updated = 0
    unchanged = 0
    rows: list[dict[str, Any]] = []
    now = utc_now()
    for card in report["cards"]:
        existing = await session.scalar(
            select(TradeCandidate).where(TradeCandidate.candidate_key == card["card_id"])
        )
        payload = _candidate_payload(card, now=now)
        if existing is None:
            session.add(TradeCandidate(**payload))
            inserted += 1
            rows.append({"candidate_key": card["card_id"], "action": "inserted", "status": payload["status"]})
            continue
        payload = _preserve_candidate_lifecycle(existing, payload)
        if _candidate_needs_update(existing, payload):
            for key, value in payload.items():
                if key == "id":
                    continue
                setattr(existing, key, value)
            updated += 1
            rows.append({"candidate_key": card["card_id"], "action": "updated", "status": payload["status"]})
        else:
            unchanged += 1
            rows.append({"candidate_key": card["card_id"], "action": "unchanged", "status": existing.status})
    if inserted or updated:
        await session.commit()
    return {
        "strategy_family": "darkflow_trade_candidates_v1",
        "requested_limit": report["requested_limit"],
        "scanned_interactions": report["scanned_interactions"],
        "card_count": report["card_count"],
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "rows": rows,
        "thresholds": report["thresholds"],
        "policy": _policy(),
    }


async def latest_materialized_trade_candidates(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_DECISION_CARD_LIMIT,
) -> dict[str, Any]:
    requested_limit = max(1, min(int(limit), 200))
    rows = await session.scalars(
        select(TradeCandidate)
        .where(TradeCandidate.lineage == "core_darkflow_v2")
        .order_by(TradeCandidate.setup_time.desc(), TradeCandidate.updated_at.desc(), TradeCandidate.id.desc())
        .limit(requested_limit)
    )
    candidates = [_materialized_candidate_payload(item) for item in rows.all()]
    return {
        "strategy_family": "darkflow_trade_candidates_v1",
        "requested_limit": requested_limit,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "policy": _policy(),
    }


def _decision_card(
    item: DarkflowInteraction,
    *,
    min_quality_score: float,
    min_rr_ratio: float,
) -> dict[str, Any] | None:
    context = item.context or {}
    if _interaction_schema(context) != DARKFLOW_INTERACTION_SCHEMA:
        return None
    if not all(isinstance(value, (int, float)) for value in (item.entry_price, item.stop_price, item.target_price)):
        return None
    entry = float(item.entry_price)
    stop = float(item.stop_price)
    target = float(item.target_price)
    if entry <= 0 or stop <= 0 or target <= 0 or entry == stop:
        return None
    quality = context.get("quality") or {}
    quality_score = _float(quality.get("score")) or 0.0
    confirmations = [str(value) for value in quality.get("confirmations") or []]
    quality_blockers = [str(value) for value in quality.get("blockers") or []]
    rr_ratio = abs(target - entry) / abs(entry - stop)
    gate_blockers = _gate_blockers(
        quality_score=quality_score,
        quality_blockers=quality_blockers,
        rr_ratio=rr_ratio,
        min_quality_score=min_quality_score,
        min_rr_ratio=min_rr_ratio,
    )
    promotion_blockers = [PROMOTION_BLOCKER_ANTI_REPAINT_MISSING, PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING]
    return {
        "card_id": f"darkflow-card:{DARKFLOW_INTERACTION_SCHEMA}:{item.interaction_key}",
        "source_interaction_id": item.id,
        "source_snapshot_id": item.source_snapshot_id,
        "strategy_id": item.playbook,
        "strategy_name": _playbook_display_name(item.playbook),
        "symbol": item.symbol,
        "timeframe": item.timeframe,
        "interval": item.interval,
        "direction": item.direction,
        "setup_type": item.interaction_type,
        "market_state": _market_state(item.playbook, item.interaction_type),
        "setup_time": _iso(item.event_ts),
        "entry_plan": _frozen_entry_plan(item, entry=entry, stop=stop, target=target),
        "scores": {
            "rule_score": round(quality_score / 10.0, 2),
            "quality_score": round(quality_score, 3),
            "model_win_prob": None,
            "expected_R": None,
            "tail_risk": None,
        },
        "risk": {
            "rr_ratio": round(rr_ratio, 4),
            "stop_distance_pct": round(abs(entry - stop) / entry, 6),
            "target_distance_pct": round(abs(target - entry) / entry, 6),
        },
        "supporting_signals": confirmations,
        "blocking_risks": quality_blockers,
        "risk_gate": {
            "status": "shadow_candidate" if not gate_blockers else "research_blocked",
            "blockers": gate_blockers,
            "paper_eligible": False,
            "live_eligible": False,
            "promotion_blockers": promotion_blockers,
        },
        "observed_backtest_result": {
            "exit_reason": item.exit_reason,
            "exit_price": float(item.exit_price) if isinstance(item.exit_price, (int, float)) else None,
            "exit_time": _iso(item.exit_ts),
            "r_multiple": float(item.r_multiple) if isinstance(item.r_multiple, (int, float)) else None,
            "mfe": float(item.mfe),
            "mae": float(item.mae),
        },
        "context": {
            "target_model": context.get("target_model") or ((context.get("target_plan") or {}).get("model")),
            "tutorial_rule_family": context.get("tutorial_rule_family"),
            "interaction_schema": _interaction_schema(context),
            "research_only": True,
        },
    }


def decision_card_from_interaction(
    item: DarkflowInteraction,
    *,
    min_quality_score: float = DEFAULT_MIN_DECISION_CARD_QUALITY,
    min_rr_ratio: float = DEFAULT_MIN_RR_RATIO,
) -> dict[str, Any] | None:
    return _decision_card(item, min_quality_score=min_quality_score, min_rr_ratio=min_rr_ratio)


def _interaction_schema(context: dict[str, Any]) -> str | None:
    raw = context.get("interaction_schema")
    if raw:
        return str(raw)
    if context.get("quality") and context.get("evidence") and context.get("target_plan"):
        return DARKFLOW_INTERACTION_SCHEMA
    return None


def _gate_blockers(
    *,
    quality_score: float,
    quality_blockers: list[str],
    rr_ratio: float,
    min_quality_score: float,
    min_rr_ratio: float,
) -> list[str]:
    blockers: list[str] = []
    if quality_score < min_quality_score:
        blockers.append("quality_score_below_threshold")
    if rr_ratio < min_rr_ratio:
        blockers.append("rr_ratio_below_threshold")
    blockers.extend(sorted(set(quality_blockers) & _HARD_QUALITY_BLOCKERS))
    return blockers


def _take_profit_levels(item: DarkflowInteraction, target: float) -> list[dict[str, Any]]:
    target_plan = (item.context or {}).get("target_plan") or {}
    return [
        {
            "label": "TP1",
            "price": target,
            "source": target_plan.get("model") or (item.context or {}).get("target_model") or "interaction_target",
        }
    ]


def _frozen_entry_plan(item: DarkflowInteraction, *, entry: float, stop: float, target: float) -> dict[str, Any]:
    context = item.context or {}
    hold_bars = int(context.get("hold_bars") or 0)
    entry_range = _entry_range_from_context(item, entry=entry, stop=stop, target=target)
    valid_until = _entry_plan_valid_until(item, max_hold_bars=hold_bars)
    invalidation_price = float(item.invalidation_price) if isinstance(item.invalidation_price, (int, float)) else stop
    return {
        "plan_type": FROZEN_ENTRY_PLAN_TYPE,
        "status": "frozen",
        "state": "frozen",
        "trigger": _entry_trigger(item),
        "planned_entry": entry,
        "planned_stop": stop,
        "take_profit_levels": _take_profit_levels(item, target),
        "invalidation_price": invalidation_price,
        "max_hold_bars": hold_bars,
        "frozen_at": _iso(item.event_ts),
        "valid_until": _iso(valid_until),
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


def _entry_range_from_context(
    item: DarkflowInteraction,
    *,
    entry: float,
    stop: float,
    target: float,
) -> dict[str, Any]:
    context = item.context or {}
    zone = context.get("zone") if isinstance(context.get("zone"), dict) else {}
    lower = _float(zone.get("lower_price")) if isinstance(zone, dict) else None
    upper = _float(zone.get("upper_price")) if isinstance(zone, dict) else None
    if lower is not None and upper is not None:
        source = "source_darkflow_zone"
        lower_value, upper_value = sorted((float(lower), float(upper)))
    else:
        source = "entry_reference_tolerance"
        width = abs(entry) * DEFAULT_ENTRY_PLAN_TOLERANCE_PCT
        lower_value = entry - width
        upper_value = entry + width
    clipped = _clip_entry_range(
        item.direction,
        lower=lower_value,
        upper=upper_value,
        entry=entry,
        stop=stop,
        target=target,
    )
    if clipped is None:
        width = abs(entry) * DEFAULT_ENTRY_PLAN_TOLERANCE_PCT
        clipped = _clip_entry_range(
            item.direction,
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


def _clip_entry_range(
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


def _entry_plan_valid_until(item: DarkflowInteraction, *, max_hold_bars: int) -> datetime:
    start = _aware(item.event_ts)
    bars = max(1, int(max_hold_bars or DEFAULT_ENTRY_PLAN_VALID_BARS))
    validity = _interval_delta(item.interval) * bars
    validity = max(validity, timedelta(hours=2))
    validity = min(validity, timedelta(hours=72))
    return start + validity


def _interval_delta(interval: str | None) -> timedelta:
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


def _entry_trigger(item: DarkflowInteraction) -> str:
    if item.interaction_type == "wick_pierce_reclaim":
        return "wick_pierce_reclaim_confirmed"
    if item.interaction_type == "first_touch":
        return "first_touch_zone_reaction"
    if item.interaction_type == "body_break":
        return "body_break_confirmation"
    return item.interaction_type


def _market_state(playbook: str, interaction_type: str) -> str:
    if playbook == "pullback_to_cost":
        return "cost_pullback"
    if playbook == "liquidity_sweep_reversal":
        return "liquidity_hunt_reversal"
    if playbook == "breakout_confirmation":
        return "structure_breakout"
    if playbook == "trend_ride_extension":
        return "trend_extension"
    if interaction_type == "body_break":
        return "invalidation_or_breakdown"
    return "darkflow_zone_reaction"


def _playbook_display_name(key: str) -> str:
    for playbook in PLAYBOOKS:
        if playbook.key == key:
            return playbook.display_name
    return key


def _policy() -> dict[str, Any]:
    return {
        "research_only": True,
        "opens_live_orders": False,
        "opens_paper_trades": False,
        "used_for_opening_decisions": False,
        "lineage": core_darkflow_v2_lineage(),
        "decision_card_contract": "read_model_only_until_trade_candidates_are_persisted",
    }


def _candidate_payload(card: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    risk_gate = card["risk_gate"]
    entry_plan = card["entry_plan"]
    target = entry_plan["take_profit_levels"][0]
    status = str(risk_gate["status"])
    promotion_blockers = _normalized_promotion_blockers(risk_gate.get("promotion_blockers") or [])
    return {
        "candidate_key": str(card["card_id"]),
        "source_type": "darkflow_interaction",
        "source_interaction_id": str(card.get("source_interaction_id") or ""),
        "lineage": "core_darkflow_v2",
        "strategy_family": "darkflow_trade_candidates_v1",
        "strategy_id": str(card["strategy_id"]),
        "strategy_name": str(card["strategy_name"]),
        "symbol": str(card["symbol"]),
        "timeframe": str(card["timeframe"]),
        "interval": str(card["interval"]),
        "direction": str(card["direction"]),
        "setup_type": str(card["setup_type"]),
        "market_state": str(card["market_state"]),
        "setup_time": _parse_iso_datetime(card.get("setup_time")),
        "entry_price": float(entry_plan["planned_entry"]),
        "stop_price": float(entry_plan["planned_stop"]),
        "target_price": float(target["price"]),
        "rr_ratio": float(card["risk"]["rr_ratio"]),
        "quality_score": float(card["scores"]["quality_score"]),
        "rule_score": float(card["scores"]["rule_score"]),
        "model_win_prob": _float(card["scores"].get("model_win_prob")),
        "expected_r": _float(card["scores"].get("expected_R")),
        "status": status,
        "promotion_status": _promotion_status(
            status=status,
            anti_repaint_status="missing",
            shadow_status="not_started",
            promotion_blockers=promotion_blockers,
        ),
        "anti_repaint_status": "missing",
        "shadow_status": "not_started",
        "paper_eligible": False,
        "live_eligible": False,
        "blockers": list(risk_gate.get("blockers") or []),
        "promotion_blockers": promotion_blockers,
        "supporting_signals": list(card.get("supporting_signals") or []),
        "decision_payload": card,
        "materialized_at": now,
        "updated_at": now,
    }


def _candidate_needs_update(existing: TradeCandidate, payload: dict[str, Any]) -> bool:
    fields = [
        "source_interaction_id",
        "strategy_id",
        "symbol",
        "direction",
        "entry_price",
        "stop_price",
        "target_price",
        "rr_ratio",
        "quality_score",
        "status",
        "promotion_status",
        "blockers",
        "promotion_blockers",
        "supporting_signals",
        "decision_payload",
    ]
    return any(getattr(existing, field) != payload[field] for field in fields)


def _preserve_candidate_lifecycle(existing: TradeCandidate, payload: dict[str, Any]) -> dict[str, Any]:
    plan_changed = any(
        getattr(existing, field) != payload[field]
        for field in ("entry_price", "stop_price", "target_price", "direction", "strategy_id")
    )
    if plan_changed:
        return payload
    preserved = dict(payload)
    for field in (
        "anti_repaint_status",
        "shadow_status",
        "paper_eligible",
        "live_eligible",
        "promotion_status",
    ):
        preserved[field] = getattr(existing, field)
    preserved["promotion_blockers"] = _merge_lifecycle_blockers(
        payload["promotion_blockers"],
        existing.promotion_blockers,
        anti_repaint_status=existing.anti_repaint_status,
        shadow_status=existing.shadow_status,
    )
    preserved["materialized_at"] = existing.materialized_at
    return preserved


def _normalized_promotion_blockers(values: list[Any]) -> list[str]:
    obsolete = {PROMOTION_BLOCKER_PERSISTENT_TABLE_MISSING}
    blockers = [str(value) for value in values if str(value) not in obsolete]
    return list(dict.fromkeys(blockers))


def _merge_lifecycle_blockers(
    payload_blockers: list[str],
    existing_blockers: list[str],
    *,
    anti_repaint_status: str,
    shadow_status: str,
) -> list[str]:
    blockers = set(_normalized_promotion_blockers(payload_blockers))
    blockers.update(_normalized_promotion_blockers(existing_blockers))
    if anti_repaint_status == "passed":
        blockers.discard(PROMOTION_BLOCKER_ANTI_REPAINT_MISSING)
        blockers.discard(PROMOTION_BLOCKER_ANTI_REPAINT_FAILED)
    elif anti_repaint_status == "failed":
        blockers.discard(PROMOTION_BLOCKER_ANTI_REPAINT_MISSING)
        blockers.add(PROMOTION_BLOCKER_ANTI_REPAINT_FAILED)
    else:
        blockers.add(PROMOTION_BLOCKER_ANTI_REPAINT_MISSING)
    if shadow_status == "passed":
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED)
    elif shadow_status == "collecting":
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED)
        blockers.add(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING)
    elif shadow_status == "failed":
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING)
        blockers.add(PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED)
    else:
        blockers.add(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
    ordered = [
        PROMOTION_BLOCKER_ANTI_REPAINT_MISSING,
        PROMOTION_BLOCKER_ANTI_REPAINT_FAILED,
        PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING,
        PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING,
        PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED,
    ]
    return [item for item in ordered if item in blockers] + sorted(blockers - set(ordered))


def _promotion_status(
    *,
    status: str,
    anti_repaint_status: str,
    shadow_status: str,
    promotion_blockers: list[str],
) -> str:
    blockers = set(promotion_blockers)
    if status != "shadow_candidate":
        return "blocked"
    if anti_repaint_status == "failed" or PROMOTION_BLOCKER_ANTI_REPAINT_FAILED in blockers:
        return "anti_repaint_failed"
    if anti_repaint_status != "passed":
        return "anti_repaint_pending"
    if shadow_status == "failed" or PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED in blockers:
        return "shadow_forward_failed"
    if shadow_status == "collecting" or PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING in blockers:
        return "shadow_forward_collecting"
    if shadow_status != "passed":
        return "shadow_forward_pending"
    return "paper_review_ready"


def _materialized_candidate_payload(item: TradeCandidate) -> dict[str, Any]:
    return {
        "id": item.id,
        "candidate_key": item.candidate_key,
        "source_type": item.source_type,
        "source_interaction_id": item.source_interaction_id,
        "lineage": item.lineage,
        "strategy_family": item.strategy_family,
        "strategy_id": item.strategy_id,
        "strategy_name": item.strategy_name,
        "symbol": item.symbol,
        "timeframe": item.timeframe,
        "interval": item.interval,
        "direction": item.direction,
        "setup_type": item.setup_type,
        "market_state": item.market_state,
        "setup_time": _iso(item.setup_time),
        "entry_price": item.entry_price,
        "stop_price": item.stop_price,
        "target_price": item.target_price,
        "rr_ratio": item.rr_ratio,
        "quality_score": item.quality_score,
        "rule_score": item.rule_score,
        "model_win_prob": item.model_win_prob,
        "expected_R": item.expected_r,
        "status": item.status,
        "promotion_status": item.promotion_status,
        "anti_repaint_status": item.anti_repaint_status,
        "shadow_status": item.shadow_status,
        "paper_eligible": item.paper_eligible,
        "live_eligible": item.live_eligible,
        "blockers": item.blockers,
        "promotion_blockers": item.promotion_blockers,
        "supporting_signals": item.supporting_signals,
        "decision_payload": item.decision_payload,
        "materialized_at": _iso(item.materialized_at),
        "updated_at": _iso(item.updated_at),
    }


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
