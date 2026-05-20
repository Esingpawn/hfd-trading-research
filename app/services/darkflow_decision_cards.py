from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.trade_candidates.lifecycle import (
    PROMOTION_BLOCKER_ANTI_REPAINT_FAILED,
    PROMOTION_BLOCKER_ANTI_REPAINT_MISSING,
    PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN,
    PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED,
    PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING,
    PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED,
    PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING,
    PROMOTION_BLOCKER_SHADOW_MARKET_PAUSED,
    candidate_promotion_status,
    lifecycle_blockers,
    normalized_promotion_blockers,
)
from app.domain.trade_candidates import entry_plan as entry_plan_rules
from app.models import DarkflowInteraction, TradeCandidate, utc_now
from app.services.darkflow_interactions import DARKFLOW_INTERACTION_SCHEMA
from app.services.darkflow_playbooks import PLAYBOOKS
from app.services.research_lineage import core_darkflow_v2_lineage


DEFAULT_DECISION_CARD_LIMIT = 20
DEFAULT_MIN_DECISION_CARD_QUALITY = 55.0
DEFAULT_MIN_SHADOW_RESEARCH_QUALITY = 50.0
DEFAULT_MIN_RR_RATIO = 1.5
DEFAULT_TRADE_CANDIDATE_LIMIT = 100
DEFAULT_TRADE_CANDIDATE_FETCH_MULTIPLIER = 3
DEFAULT_TRADE_CANDIDATE_OPENING_FETCH_MULTIPLIER = 10
DEFAULT_TRADE_CANDIDATE_NON_OPENING_FETCH_MULTIPLIER = 2
NON_OPENING_PLAYBOOK_POLICIES = {"research_only_exit_filter_not_opening"}
FROZEN_ENTRY_PLAN_TYPE = entry_plan_rules.FROZEN_ENTRY_PLAN_TYPE
DEFAULT_ENTRY_PLAN_VALID_BARS = entry_plan_rules.DEFAULT_ENTRY_PLAN_VALID_BARS
DEFAULT_ENTRY_PLAN_TOLERANCE_PCT = entry_plan_rules.DEFAULT_ENTRY_PLAN_TOLERANCE_PCT
DEFAULT_ENTRY_PLAN_DRIFT_LIMIT_PCT = entry_plan_rules.DEFAULT_ENTRY_PLAN_DRIFT_LIMIT_PCT
_HARD_QUALITY_BLOCKERS = {
    "body_break_invalidation",
    "official_rule_unmapped",
    "exit_filter_not_opening_playbook",
    "parent_trend_conflict",
}
_SHADOW_RESEARCH_ONLY_BLOCKERS = {"rr_ratio_below_threshold"}


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


async def _latest_materialization_decision_cards(
    session: AsyncSession,
    *,
    limit: int,
    min_quality_score: float,
    min_rr_ratio: float,
) -> dict[str, Any]:
    requested_limit = max(1, int(limit))
    opening_fetch_limit = _materialize_opening_card_fetch_limit(requested_limit)
    non_opening_fetch_limit = _materialize_non_opening_card_fetch_limit(requested_limit)
    opening_cards, opening_scanned, opening_skipped = await _decision_cards_from_interactions(
        session,
        limit=opening_fetch_limit,
        min_quality_score=min_quality_score,
        min_rr_ratio=min_rr_ratio,
        playbooks=_opening_playbook_keys(),
    )
    non_opening_cards, non_opening_scanned, non_opening_skipped = await _decision_cards_from_interactions(
        session,
        limit=non_opening_fetch_limit,
        min_quality_score=min_quality_score,
        min_rr_ratio=min_rr_ratio,
        playbooks=_non_opening_playbook_keys(),
    )
    return {
        "strategy_family": "darkflow_trade_decision_cards_v1",
        "interaction_schema": DARKFLOW_INTERACTION_SCHEMA,
        "requested_limit": requested_limit,
        "card_count": len(opening_cards) + len(non_opening_cards),
        "opening_card_count": len(opening_cards),
        "non_opening_card_count": len(non_opening_cards),
        "opening_card_fetch_limit": opening_fetch_limit,
        "non_opening_card_fetch_limit": non_opening_fetch_limit,
        "scanned_interactions": opening_scanned + non_opening_scanned,
        "opening_scanned_interactions": opening_scanned,
        "non_opening_scanned_interactions": non_opening_scanned,
        "cards": opening_cards + non_opening_cards,
        "skipped": (opening_skipped + non_opening_skipped)[:20],
        "thresholds": {
            "min_quality_score": float(min_quality_score),
            "min_rr_ratio": float(min_rr_ratio),
        },
        "policy": _policy() | {
            "materialization_prefers_opening_playbooks": True,
            "opening_playbooks": _opening_playbook_keys(),
            "non_opening_playbooks": _non_opening_playbook_keys(),
        },
    }


async def _decision_cards_from_interactions(
    session: AsyncSession,
    *,
    limit: int,
    min_quality_score: float,
    min_rr_ratio: float,
    playbooks: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    query = select(DarkflowInteraction).where(
        DarkflowInteraction.status == "backtested",
        DarkflowInteraction.entry_price.isnot(None),
        DarkflowInteraction.stop_price.isnot(None),
        DarkflowInteraction.target_price.isnot(None),
    )
    if playbooks is not None:
        if not playbooks:
            return [], 0, []
        query = query.where(DarkflowInteraction.playbook.in_(playbooks))
    rows = await session.scalars(
        query.order_by(DarkflowInteraction.event_ts.desc(), DarkflowInteraction.id.desc()).limit(max(1, int(limit)))
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
    return cards, scanned, skipped


async def materialize_darkflow_trade_candidates(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_TRADE_CANDIDATE_LIMIT,
    min_quality_score: float = DEFAULT_MIN_DECISION_CARD_QUALITY,
    min_rr_ratio: float = DEFAULT_MIN_RR_RATIO,
    retire_expired_entry_plans: bool = False,
    priority_subportfolio_keys: list[str] | set[str] | tuple[str, ...] | None = None,
    paused_subportfolio_keys: list[str] | set[str] | tuple[str, ...] | None = None,
    whitelist_subportfolio_keys: list[str] | set[str] | tuple[str, ...] | None = None,
    deweighted_strategy_ids: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    requested_limit = max(1, int(limit))
    card_fetch_limit = _materialize_card_fetch_limit(requested_limit)
    report = await _latest_materialization_decision_cards(
        session,
        limit=requested_limit,
        min_quality_score=min_quality_score,
        min_rr_ratio=min_rr_ratio,
    )
    inserted = 0
    updated = 0
    unchanged = 0
    skipped_non_opening = 0
    expired_entry_plan_retired = 0
    sampling_policy_skipped = 0
    sampling_policy_retired = 0
    sampling_policy_prioritized = 0
    rows: list[dict[str, Any]] = []
    now = utc_now()
    priority_subportfolios = _normalized_group_keys(priority_subportfolio_keys)
    paused_subportfolios = _normalized_group_keys(paused_subportfolio_keys)
    whitelist_subportfolios = _normalized_group_keys(whitelist_subportfolio_keys)
    deweighted_strategies = _normalized_group_keys(deweighted_strategy_ids)
    cards = _sort_cards_by_materialization_policy(
        list(report["cards"]),
        priority_subportfolio_keys=priority_subportfolios,
        paused_subportfolio_keys=paused_subportfolios,
        whitelist_subportfolio_keys=whitelist_subportfolios,
        deweighted_strategy_ids=deweighted_strategies,
    )
    representatives = _representative_exposure_candidates(cards)
    for card in cards:
        existing = await session.scalar(
            select(TradeCandidate).where(TradeCandidate.candidate_key == card["card_id"])
        )
        if _non_opening_playbook_card(card):
            skipped_non_opening += 1
            if existing is not None and _retire_non_opening_candidate(existing, card=card, now=now):
                updated += 1
                rows.append({
                    "candidate_key": card["card_id"],
                    "action": "retired_non_opening_playbook",
                    "status": existing.status,
                    "promotion_status": existing.promotion_status,
                    "duplicate_of": None,
                })
            else:
                rows.append({
                    "candidate_key": card["card_id"],
                    "action": "skipped_non_opening_playbook",
                    "status": "not_materialized",
                    "promotion_status": "not_materialized",
                    "duplicate_of": None,
                })
            continue
        sampling_gate = _materialization_sampling_gate(
            card,
            priority_subportfolio_keys=priority_subportfolios,
            paused_subportfolio_keys=paused_subportfolios,
            whitelist_subportfolio_keys=whitelist_subportfolios,
            deweighted_strategy_ids=deweighted_strategies,
        )
        if sampling_gate["decision"] in {"blocked", "deweighted"}:
            sampling_policy_skipped += 1
            if existing is not None and _retire_sampling_policy_candidate(existing, card=card, gate=sampling_gate, now=now):
                updated += 1
                sampling_policy_retired += 1
                rows.append({
                    "candidate_key": card["card_id"],
                    "action": "retired_sampling_policy",
                    "status": existing.status,
                    "promotion_status": existing.promotion_status,
                    "duplicate_of": None,
                    "sampling_gate": sampling_gate,
                })
            else:
                rows.append({
                    "candidate_key": card["card_id"],
                    "action": "skipped_sampling_policy",
                    "status": "not_materialized" if existing is None else existing.status,
                    "promotion_status": "not_materialized" if existing is None else existing.promotion_status,
                    "duplicate_of": None,
                    "sampling_gate": sampling_gate,
                })
            continue
        if sampling_gate["decision"] == "prioritize":
            sampling_policy_prioritized += 1
        exposure_fingerprint = _exposure_plan_fingerprint(card)
        duplicate_of = None
        if exposure_fingerprint is not None:
            representative = representatives.get(exposure_fingerprint)
            representative_key = str(representative.get("card_id")) if representative else None
            if representative_key and representative_key != str(card["card_id"]):
                duplicate_of = representative_key
        payload = _candidate_payload(
            card,
            now=now,
            duplicate_of=duplicate_of,
            exposure_fingerprint=exposure_fingerprint,
        )
        payload = _attach_sampling_gate(payload, sampling_gate)
        if retire_expired_entry_plans and _entry_plan_expired(card.get("entry_plan"), now=now):
            payload = _retired_entry_plan_payload(payload, reason="entry_plan_expired", entry_plan_state=None, now=now)
            expired_entry_plan_retired += 1
        if existing is None:
            session.add(TradeCandidate(**payload))
            inserted += 1
            rows.append({
                "candidate_key": card["card_id"],
                "action": "inserted",
                "status": payload["status"],
                "promotion_status": payload["promotion_status"],
                "duplicate_of": duplicate_of,
            })
            continue
        payload = _preserve_candidate_lifecycle(existing, payload)
        if _candidate_needs_update(existing, payload):
            for key, value in payload.items():
                if key == "id":
                    continue
                setattr(existing, key, value)
            updated += 1
            rows.append({
                "candidate_key": card["card_id"],
                "action": "updated",
                "status": payload["status"],
                "promotion_status": payload["promotion_status"],
                "duplicate_of": duplicate_of,
            })
        else:
            unchanged += 1
            rows.append({
                "candidate_key": card["card_id"],
                "action": "unchanged",
                "status": existing.status,
                "promotion_status": existing.promotion_status,
                "duplicate_of": duplicate_of,
            })
    if inserted or updated:
        await session.commit()
    return {
        "strategy_family": "darkflow_trade_candidates_v1",
        "requested_limit": requested_limit,
        "card_fetch_limit": card_fetch_limit,
        "opening_card_fetch_limit": report.get("opening_card_fetch_limit"),
        "non_opening_card_fetch_limit": report.get("non_opening_card_fetch_limit"),
        "scanned_interactions": report["scanned_interactions"],
        "opening_scanned_interactions": report.get("opening_scanned_interactions"),
        "non_opening_scanned_interactions": report.get("non_opening_scanned_interactions"),
        "card_count": report["card_count"],
        "opening_card_count": report.get("opening_card_count", 0),
        "non_opening_card_count": report.get("non_opening_card_count", 0),
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "skipped_non_opening_count": skipped_non_opening,
        "expired_entry_plan_retired_count": expired_entry_plan_retired,
        "sampling_policy_skipped_count": sampling_policy_skipped,
        "sampling_policy_retired_count": sampling_policy_retired,
        "sampling_policy_prioritized_count": sampling_policy_prioritized,
        "duplicate_exposure_count": sum(1 for row in rows if row.get("duplicate_of")),
        "rows": rows,
        "thresholds": report["thresholds"],
        "policy": _policy() | {
            "uses_subportfolio_sampling_policy": True,
            "sampling_policy_report_only": False,
            "sampling_policy_blocks_paused_subportfolios": True,
            "sampling_policy_blocks_deweighted_non_whitelist": True,
            "sampling_policy_opens_paper_trades": False,
            "sampling_policy_opens_live_orders": False,
        },
}


def _materialize_card_fetch_limit(limit: int) -> int:
    requested = max(1, int(limit))
    return min(max(requested * DEFAULT_TRADE_CANDIDATE_FETCH_MULTIPLIER, requested), 1000)


def _materialize_opening_card_fetch_limit(limit: int) -> int:
    requested = max(1, int(limit))
    return min(max(requested * DEFAULT_TRADE_CANDIDATE_OPENING_FETCH_MULTIPLIER, requested + 500), 5000)


def _materialize_non_opening_card_fetch_limit(limit: int) -> int:
    requested = max(1, int(limit))
    return min(max(requested * DEFAULT_TRADE_CANDIDATE_NON_OPENING_FETCH_MULTIPLIER, requested), 1000)


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
    playbook_policy = _playbook_policy(item.playbook)
    if playbook_policy in NON_OPENING_PLAYBOOK_POLICIES and "exit_filter_not_opening_playbook" not in quality_blockers:
        quality_blockers.append("exit_filter_not_opening_playbook")
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
            "status": "shadow_candidate"
            if not _shadow_sampling_blockers(gate_blockers, quality_score=quality_score, strategy_id=item.playbook)
            else "research_blocked",
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
            "playbook_policy": playbook_policy,
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


def _shadow_sampling_blockers(blockers: list[str], *, quality_score: float | None = None, strategy_id: str | None = None) -> list[str]:
    research_only = set(_SHADOW_RESEARCH_ONLY_BLOCKERS)
    if isinstance(quality_score, (int, float)) and float(quality_score) >= DEFAULT_MIN_SHADOW_RESEARCH_QUALITY:
        research_only.add("quality_score_below_threshold")
    if strategy_id == "liquidity_sweep_reversal" and isinstance(quality_score, (int, float)) and float(quality_score) >= DEFAULT_MIN_DECISION_CARD_QUALITY:
        research_only.add("parent_trend_conflict")
    return [blocker for blocker in blockers if blocker not in research_only]


def _non_opening_playbook_card(card: dict[str, Any]) -> bool:
    context = card.get("context") if isinstance(card.get("context"), dict) else {}
    return str(context.get("playbook_policy") or "") in NON_OPENING_PLAYBOOK_POLICIES


def _opening_playbook_keys() -> list[str]:
    return [playbook.key for playbook in PLAYBOOKS if playbook.policy not in NON_OPENING_PLAYBOOK_POLICIES]


def _non_opening_playbook_keys() -> list[str]:
    return [playbook.key for playbook in PLAYBOOKS if playbook.policy in NON_OPENING_PLAYBOOK_POLICIES]


def _take_profit_levels(item: DarkflowInteraction, target: float) -> list[dict[str, Any]]:
    return entry_plan_rules.take_profit_levels(context=item.context or {}, target=target)


def _frozen_entry_plan(item: DarkflowInteraction, *, entry: float, stop: float, target: float) -> dict[str, Any]:
    return entry_plan_rules.build_frozen_entry_plan(
        direction=item.direction,
        interaction_type=item.interaction_type,
        interval=item.interval,
        event_ts=item.event_ts,
        context=item.context or {},
        entry=entry,
        stop=stop,
        target=target,
        invalidation_price=item.invalidation_price,
    )


def _entry_range_from_context(
    item: DarkflowInteraction,
    *,
    entry: float,
    stop: float,
    target: float,
) -> dict[str, Any]:
    return entry_plan_rules.entry_range_from_context(
        direction=item.direction,
        context=item.context or {},
        entry=entry,
        stop=stop,
        target=target,
    )


def _clip_entry_range(
    direction: str,
    *,
    lower: float,
    upper: float,
    entry: float,
    stop: float,
    target: float,
) -> tuple[float, float] | None:
    return entry_plan_rules.clip_entry_range(direction, lower=lower, upper=upper, entry=entry, stop=stop, target=target)


def _entry_plan_valid_until(item: DarkflowInteraction, *, max_hold_bars: int) -> datetime:
    return entry_plan_rules.entry_plan_valid_until(
        event_ts=item.event_ts,
        interval=item.interval,
        max_hold_bars=max_hold_bars,
    )


def _interval_delta(interval: str | None) -> timedelta:
    return entry_plan_rules.interval_delta(interval)


def _entry_trigger(item: DarkflowInteraction) -> str:
    return entry_plan_rules.entry_trigger(item.interaction_type)


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


def _playbook_policy(key: str) -> str | None:
    for playbook in PLAYBOOKS:
        if playbook.key == key:
            return playbook.policy
    return None


def _policy() -> dict[str, Any]:
    return {
        "research_only": True,
        "opens_live_orders": False,
        "opens_paper_trades": False,
        "used_for_opening_decisions": False,
        "lineage": core_darkflow_v2_lineage(),
        "decision_card_contract": "read_model_only_until_trade_candidates_are_persisted",
    }


def _candidate_payload(
    card: dict[str, Any],
    *,
    now: datetime,
    duplicate_of: str | None = None,
    exposure_fingerprint: str | None = None,
) -> dict[str, Any]:
    risk_gate = card["risk_gate"]
    entry_plan = card["entry_plan"]
    target = entry_plan["take_profit_levels"][0]
    status = str(risk_gate["status"])
    promotion_blockers = _normalized_promotion_blockers(risk_gate.get("promotion_blockers") or [])
    decision_payload = dict(card)
    if exposure_fingerprint:
        decision_payload["exposure_plan_fingerprint"] = exposure_fingerprint
    if duplicate_of:
        status = "entry_plan_retired"
        promotion_blockers = _normalized_promotion_blockers([PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN])
        decision_payload["duplicate_shadow_plan"] = {
            "duplicate_of": duplicate_of,
            "reason": "same_frozen_entry_exposure",
            "retired_at": _iso(now),
        }
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
            anti_repaint_status="passed" if duplicate_of else "missing",
            shadow_status="retired" if duplicate_of else "not_started",
            promotion_blockers=promotion_blockers,
        ),
        "anti_repaint_status": "passed" if duplicate_of else "missing",
        "shadow_status": "retired" if duplicate_of else "not_started",
        "paper_eligible": False,
        "live_eligible": False,
        "blockers": list(risk_gate.get("blockers") or []),
        "promotion_blockers": promotion_blockers,
        "supporting_signals": list(card.get("supporting_signals") or []),
        "decision_payload": decision_payload,
        "materialized_at": now,
        "updated_at": now,
    }


def _retired_entry_plan_payload(
    payload: dict[str, Any],
    *,
    reason: str,
    entry_plan_state: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    retired = dict(payload)
    decision_payload = dict(retired.get("decision_payload") or {})
    decision_payload["entry_plan_retirement"] = {
        "reason": reason,
        "retired_at": _iso(now),
        "entry_plan_state": entry_plan_state or {},
    }
    promotion_blockers = set(_normalized_promotion_blockers(retired.get("promotion_blockers") or []))
    promotion_blockers.add(PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED)
    retired["status"] = "entry_plan_retired"
    retired["anti_repaint_status"] = "passed"
    retired["shadow_status"] = "retired"
    retired["promotion_blockers"] = _normalized_promotion_blockers(list(promotion_blockers))
    retired["promotion_status"] = _promotion_status(
        status=retired["status"],
        anti_repaint_status=retired["anti_repaint_status"],
        shadow_status=retired["shadow_status"],
        promotion_blockers=retired["promotion_blockers"],
    )
    retired["decision_payload"] = decision_payload
    retired["updated_at"] = now
    return retired


def _entry_plan_expired(plan: Any, *, now: datetime) -> bool:
    if not isinstance(plan, dict):
        return False
    valid_until = _parse_iso_datetime(plan.get("valid_until"))
    return valid_until is not None and _aware(now) > _aware(valid_until)


def _sort_cards_by_materialization_policy(
    cards: list[dict[str, Any]],
    *,
    priority_subportfolio_keys: set[str],
    paused_subportfolio_keys: set[str],
    whitelist_subportfolio_keys: set[str],
    deweighted_strategy_ids: set[str],
) -> list[dict[str, Any]]:
    def rank(card: dict[str, Any]) -> tuple[int, datetime, str]:
        gate = _materialization_sampling_gate(
            card,
            priority_subportfolio_keys=priority_subportfolio_keys,
            paused_subportfolio_keys=paused_subportfolio_keys,
            whitelist_subportfolio_keys=whitelist_subportfolio_keys,
            deweighted_strategy_ids=deweighted_strategy_ids,
        )
        decision_rank = {
            "prioritize": 3,
            "neutral": 2,
            "deweighted": 1,
            "blocked": 0,
        }.get(str(gate["decision"]), 2)
        return (
            decision_rank,
            _parse_iso_datetime(card.get("setup_time")) or datetime.min.replace(tzinfo=timezone.utc),
            str(card.get("card_id") or ""),
        )

    return sorted(cards, key=rank, reverse=True)


def _materialization_sampling_gate(
    card: dict[str, Any],
    *,
    priority_subportfolio_keys: set[str],
    paused_subportfolio_keys: set[str],
    whitelist_subportfolio_keys: set[str],
    deweighted_strategy_ids: set[str],
) -> dict[str, Any]:
    subportfolio_key = _card_subportfolio_key(card)
    strategy_id = str(card.get("strategy_id") or "")
    if subportfolio_key in paused_subportfolio_keys:
        return {
            "decision": "blocked",
            "reason": "darkflow_subportfolio_recommendation_paused",
            "subportfolio_key": subportfolio_key,
            "strategy_id": strategy_id,
        }
    if strategy_id in deweighted_strategy_ids and subportfolio_key not in whitelist_subportfolio_keys:
        return {
            "decision": "deweighted",
            "reason": "darkflow_strategy_deweighted_non_whitelist",
            "subportfolio_key": subportfolio_key,
            "strategy_id": strategy_id,
        }
    if subportfolio_key in priority_subportfolio_keys:
        return {
            "decision": "prioritize",
            "reason": "darkflow_subportfolio_whitelist",
            "subportfolio_key": subportfolio_key,
            "strategy_id": strategy_id,
        }
    return {
        "decision": "neutral",
        "reason": "darkflow_subportfolio_unclassified",
        "subportfolio_key": subportfolio_key,
        "strategy_id": strategy_id,
    }


def _card_subportfolio_key(card: dict[str, Any]) -> str:
    return "|".join(
        [
            str(card.get("strategy_id") or ""),
            str(card.get("symbol") or ""),
            str(card.get("direction") or ""),
            str(card.get("market_state") or ""),
        ]
    )


def _attach_sampling_gate(payload: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
    if gate.get("decision") == "neutral":
        return payload
    updated = dict(payload)
    decision_payload = dict(updated.get("decision_payload") or {})
    decision_payload["darkflow_materialization_gate"] = gate
    updated["decision_payload"] = decision_payload
    return updated


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


def _retire_non_opening_candidate(existing: TradeCandidate, *, card: dict[str, Any], now: datetime) -> bool:
    previous = (
        existing.status,
        existing.promotion_status,
        existing.anti_repaint_status,
        existing.shadow_status,
        tuple(existing.blockers or []),
        tuple(existing.promotion_blockers or []),
        existing.decision_payload,
    )
    payload = dict(existing.decision_payload or {})
    payload.update(dict(card))
    payload["non_opening_playbook_retirement"] = {
        "reason": "playbook_policy_not_opening",
        "playbook_policy": (card.get("context") or {}).get("playbook_policy") if isinstance(card.get("context"), dict) else None,
        "retired_at": _iso(now),
    }
    blockers = set(str(item) for item in (existing.blockers or []))
    blockers.add("exit_filter_not_opening_playbook")
    promotion_blockers = set(str(item) for item in (existing.promotion_blockers or []))
    promotion_blockers.add(PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED)
    existing.status = "entry_plan_retired"
    existing.anti_repaint_status = "passed"
    existing.shadow_status = "retired"
    existing.blockers = sorted(blockers)
    existing.promotion_blockers = _normalized_promotion_blockers(list(promotion_blockers))
    existing.promotion_status = _promotion_status(
        status=existing.status,
        anti_repaint_status=existing.anti_repaint_status,
        shadow_status=existing.shadow_status,
        promotion_blockers=existing.promotion_blockers,
    )
    existing.decision_payload = payload
    existing.updated_at = now
    return previous != (
        existing.status,
        existing.promotion_status,
        existing.anti_repaint_status,
        existing.shadow_status,
        tuple(existing.blockers or []),
        tuple(existing.promotion_blockers or []),
        existing.decision_payload,
    )


def _retire_sampling_policy_candidate(existing: TradeCandidate, *, card: dict[str, Any], gate: dict[str, Any], now: datetime) -> bool:
    previous = (
        existing.status,
        existing.promotion_status,
        existing.anti_repaint_status,
        existing.shadow_status,
        tuple(existing.blockers or []),
        tuple(existing.promotion_blockers or []),
        existing.decision_payload,
    )
    payload = dict(existing.decision_payload or {})
    payload.update(dict(card))
    payload["darkflow_materialization_gate"] = gate | {"retired_at": _iso(now)}
    promotion_blockers = set(str(item) for item in (existing.promotion_blockers or []))
    promotion_blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
    promotion_blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING)
    promotion_blockers.add(PROMOTION_BLOCKER_SHADOW_MARKET_PAUSED)
    existing.status = "entry_plan_retired"
    existing.anti_repaint_status = "passed"
    existing.shadow_status = "retired"
    existing.promotion_blockers = _normalized_promotion_blockers(list(promotion_blockers))
    existing.promotion_status = _promotion_status(
        status=existing.status,
        anti_repaint_status=existing.anti_repaint_status,
        shadow_status=existing.shadow_status,
        promotion_blockers=existing.promotion_blockers,
    )
    existing.decision_payload = payload
    existing.updated_at = now
    return previous != (
        existing.status,
        existing.promotion_status,
        existing.anti_repaint_status,
        existing.shadow_status,
        tuple(existing.blockers or []),
        tuple(existing.promotion_blockers or []),
        existing.decision_payload,
    )


def _representative_exposure_candidates(cards: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    representatives: dict[str, dict[str, Any]] = {}
    for card in cards:
        fingerprint = _exposure_plan_fingerprint(card)
        if fingerprint is None:
            continue
        current = representatives.get(fingerprint)
        if current is None or _representative_rank(card) > _representative_rank(current):
            representatives[fingerprint] = card
    return representatives


def _representative_rank(card: dict[str, Any]) -> tuple[int, float, float, str]:
    scores = card.get("scores") if isinstance(card.get("scores"), dict) else {}
    risk = card.get("risk") if isinstance(card.get("risk"), dict) else {}
    risk_gate = card.get("risk_gate") if isinstance(card.get("risk_gate"), dict) else {}
    blockers = [str(value) for value in risk_gate.get("blockers") or []]
    quality_score = _float(scores.get("quality_score")) or 0.0
    sampleable_rank = 1 if not _shadow_sampling_blockers(blockers, quality_score=quality_score, strategy_id=str(card.get("strategy_id") or "")) else 0
    return (
        sampleable_rank,
        quality_score,
        _float(risk.get("rr_ratio")) or 0.0,
        str(card.get("card_id") or ""),
    )


def _exposure_plan_fingerprint(card: dict[str, Any]) -> str | None:
    entry_plan = card.get("entry_plan") if isinstance(card.get("entry_plan"), dict) else {}
    entry = _float(entry_plan.get("planned_entry"))
    stop = _float(entry_plan.get("planned_stop"))
    if entry is None or stop is None or entry <= 0 or stop <= 0:
        return None
    return ":".join(
        [
            str(card.get("strategy_id") or ""),
            str(card.get("symbol") or ""),
            str(card.get("timeframe") or ""),
            str(card.get("interval") or ""),
            str(card.get("direction") or ""),
            str(card.get("setup_time") or ""),
            _rounded_price_bucket(entry),
            _rounded_price_bucket(stop),
        ]
    )


def _rounded_price_bucket(price: float) -> str:
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


def _normalized_group_keys(values: list[str] | set[str] | tuple[str, ...] | None) -> set[str]:
    return {str(item) for item in (values or []) if str(item)}


def _preserve_candidate_lifecycle(existing: TradeCandidate, payload: dict[str, Any]) -> dict[str, Any]:
    plan_changed = any(
        getattr(existing, field) != payload[field]
        for field in ("entry_price", "stop_price", "target_price", "direction", "strategy_id")
    )
    if plan_changed:
        return payload
    duplicate_payload = payload.get("shadow_status") == "retired" and PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN in set(
        payload.get("promotion_blockers") or []
    )
    retired_payload = payload.get("shadow_status") == "retired" and PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED in set(
        payload.get("promotion_blockers") or []
    )
    preserved = dict(payload)
    for field in (
        "anti_repaint_status",
        "shadow_status",
        "paper_eligible",
        "live_eligible",
        "promotion_status",
    ):
        preserved[field] = getattr(existing, field)
    lifecycle_anti_repaint = existing.anti_repaint_status
    lifecycle_shadow = existing.shadow_status
    if duplicate_payload or retired_payload:
        preserved["status"] = payload["status"]
        preserved["shadow_status"] = payload["shadow_status"]
        preserved["promotion_status"] = payload["promotion_status"]
        preserved["anti_repaint_status"] = payload["anti_repaint_status"]
        preserved["decision_payload"] = payload["decision_payload"]
        lifecycle_anti_repaint = payload["anti_repaint_status"]
        lifecycle_shadow = payload["shadow_status"]
    elif existing.shadow_status == "retired" or existing.status == "entry_plan_retired":
        preserved["status"] = existing.status
        preserved["decision_payload"] = existing.decision_payload
    preserved["promotion_blockers"] = _merge_lifecycle_blockers(
        payload["promotion_blockers"],
        existing.promotion_blockers,
        anti_repaint_status=lifecycle_anti_repaint,
        shadow_status=lifecycle_shadow,
    )
    if duplicate_payload or retired_payload:
        preserved["promotion_status"] = _promotion_status(
            status=preserved["status"],
            anti_repaint_status=preserved["anti_repaint_status"],
            shadow_status=preserved["shadow_status"],
            promotion_blockers=preserved["promotion_blockers"],
        )
    preserved["materialized_at"] = existing.materialized_at
    return preserved


def _normalized_promotion_blockers(values: list[Any]) -> list[str]:
    return normalized_promotion_blockers(values)


def _merge_lifecycle_blockers(
    payload_blockers: list[str],
    existing_blockers: list[str],
    *,
    anti_repaint_status: str,
    shadow_status: str,
) -> list[str]:
    blockers = set(_normalized_promotion_blockers(payload_blockers))
    blockers.update(_normalized_promotion_blockers(existing_blockers))
    return lifecycle_blockers(
        blockers,
        anti_repaint_status=anti_repaint_status,
        shadow_status=shadow_status,
    )


def _promotion_status(
    *,
    status: str,
    anti_repaint_status: str,
    shadow_status: str,
    promotion_blockers: list[str],
) -> str:
    return candidate_promotion_status(
        status=status,
        anti_repaint_status=anti_repaint_status,
        shadow_status=shadow_status,
        promotion_blockers=promotion_blockers,
    )


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
