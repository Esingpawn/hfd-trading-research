from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.trade_candidates.lifecycle import (
    CandidateLifecycleEvidence,
    AntiRepaintEvidence,
    ShadowForwardEvidence,
    PROMOTION_BLOCKER_ANTI_REPAINT_FAILED,
    PROMOTION_BLOCKER_ANTI_REPAINT_MISSING,
    PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN,
    PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED,
    PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING,
    PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED,
    PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING,
    PROMOTION_BLOCKER_SHADOW_MARKET_PAUSED,
    candidate_promotion_status,
    decide_candidate_lifecycle,
    ordered_promotion_blockers,
)
from app.domain.trade_candidates import entry_plan as entry_plan_rules
from app.domain.trade_candidates.promotion_gate import (
    PromotionGateEvidence,
    decide_promotion_gate,
    promotion_gate_policy,
)
from app.models import DarkflowInteraction, ExperimentRun, PriceSnapshot, ShadowPaperTrade, SignalSnapshot, TradeCandidate, utc_now
from app.services.darkflow_decision_cards import (
    decision_card_from_interaction,
    materialize_darkflow_trade_candidates,
)
DARKFLOW_V2_SHADOW_STRATEGY_NAME = "darkflow_v2_trade_candidate_shadow_forward_v1"
DEFAULT_PROMOTION_LIMIT = 500
DEFAULT_SHADOW_FORWARD_LIMIT = 100
DEFAULT_SHADOW_FORWARD_SCAN_MULTIPLIER = 5
DEFAULT_MAX_CANDIDATE_AGE_HOURS = 72.0
DEFAULT_ENTRY_TOLERANCE_PCT = 0.025
DEFAULT_PAUSED_GROUP_EXPLORATION_LIMIT = 1
DEFAULT_MAX_OPEN_SHADOW_TRADES_PER_MARKET = 3
SHADOW_FEE_RATE = 0.0004
SHADOW_SLIPPAGE_RATE_BY_TIER = {
    "core": 0.0002,
    "mainstream": 0.00035,
    "high_volatility": 0.0007,
}
SHADOW_FORWARD_MIN_CLOSED_TRADES = 10
SHADOW_FORWARD_MIN_WIN_RATE = 0.52
SHADOW_FORWARD_MIN_PROFIT_FACTOR = 1.15
SHADOW_FORWARD_MAX_DRAWDOWN = 0.12
SHADOW_MARKET_MIN_CLOSED_TRADES = 3
SHADOW_MARKET_PAUSE_MAX_WIN_RATE = 0.35
SHADOW_MARKET_PAUSE_MAX_PROFIT_FACTOR = 0.8
SHADOW_MARKET_PRIORITY_MIN_WIN_RATE = 0.55
SHADOW_MARKET_PRIORITY_MIN_PROFIT_FACTOR = 1.15
_TERMINAL_PLAN_CHECK_REASONS = {
    "unsupported_direction",
    "invalid_plan_prices",
    "invalid_long_reward_shape",
    "invalid_short_reward_shape",
    "stale_candidate",
}
_TERMINAL_ENTRY_PLAN_STATES = {"expired", "missed", "invalidated", "invalid_shape"}


async def audit_darkflow_trade_candidates(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_PROMOTION_LIMIT,
    include_blocked: bool = False,
) -> dict[str, Any]:
    rows = await _candidate_rows(session, limit=limit, include_blocked=include_blocked, prioritize_pending_shadow_audit=True)
    passed = 0
    failed = 0
    skipped: list[dict[str, Any]] = []
    audited: list[dict[str, Any]] = []
    now = utc_now()
    for candidate in rows:
        if candidate.status != "shadow_candidate" and not include_blocked:
            skipped.append({"candidate_key": candidate.candidate_key, "reason": "not_shadow_candidate"})
            continue
        source = await _source_interaction(session, candidate)
        result = _anti_repaint_audit(candidate, source)
        candidate.anti_repaint_status = "passed" if result["passed"] else "failed"
        candidate.updated_at = now
        _apply_lifecycle(candidate)
        if result["passed"]:
            passed += 1
        else:
            failed += 1
        audited.append(
            {
                "candidate_key": candidate.candidate_key,
                "anti_repaint_status": candidate.anti_repaint_status,
                "promotion_status": candidate.promotion_status,
                "checks": result["checks"],
                "failures": result["failures"],
            }
        )
    if audited:
        await session.commit()
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "requested_limit": max(1, int(limit)),
        "audited": len(audited),
        "passed": passed,
        "failed": failed,
        "skipped": skipped[:50],
        "rows": audited[:100],
        "policy": _policy(),
    }


async def open_darkflow_shadow_forward_samples(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_SHADOW_FORWARD_LIMIT,
    max_candidate_age_hours: float = DEFAULT_MAX_CANDIDATE_AGE_HOURS,
    entry_tolerance_pct: float = DEFAULT_ENTRY_TOLERANCE_PCT,
    priority_group_keys: list[str] | set[str] | tuple[str, ...] | None = None,
    paused_group_keys: list[str] | set[str] | tuple[str, ...] | None = None,
    paused_group_exploration_limit: int = DEFAULT_PAUSED_GROUP_EXPLORATION_LIMIT,
) -> dict[str, Any]:
    requested_limit = max(1, int(limit))
    scan_limit = _shadow_candidate_scan_limit(requested_limit)
    rows = await _shadow_ready_candidate_rows(session, limit=scan_limit)
    priority_groups = _normalized_group_keys(priority_group_keys)
    paused_groups = _normalized_group_keys(paused_group_keys)
    paused_group_exploration_budget = _paused_group_exploration_budget(paused_groups, paused_group_exploration_limit)
    if priority_groups or paused_groups:
        rows = _sort_candidates_by_alpha_sampling_plan(rows, priority_groups=priority_groups, paused_groups=paused_groups)
    market_stats = await _shadow_market_performance_stats(session)
    stats_by_candidate = await _shadow_stats_by_candidate(session, [candidate.candidate_key for candidate in rows])
    latest_prices = await _latest_prices(session, sorted({candidate.symbol for candidate in rows}))
    open_plan_index = await _open_shadow_plan_index(session, rows)
    open_market_counts = _open_shadow_market_counts(open_plan_index)
    opened: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    now = utc_now()
    scanned = 0
    for candidate in rows:
        scanned += 1
        alpha_group_key = _candidate_alpha_group_key(candidate)
        alpha_exploration_group_key = None
        if alpha_group_key in paused_groups:
            if paused_group_exploration_budget.get(alpha_group_key, 0) > 0:
                alpha_exploration_group_key = alpha_group_key
            else:
                if _pause_shadow_candidate_for_alpha_group(candidate, group_key=alpha_group_key, now=now):
                    updated.append(_candidate_update_row(candidate, _candidate_stats(stats_by_candidate, candidate), reason="alpha_group_shadow_performance_paused"))
                skipped.append(
                    {
                        "candidate_key": candidate.candidate_key,
                        "symbol": candidate.symbol,
                        "direction": candidate.direction,
                        "reason": "alpha_group_shadow_performance_paused",
                        "alpha_group_key": alpha_group_key,
                    }
                )
                continue
        market_gate = _shadow_market_gate_from_stats(candidate, market_stats)
        if market_gate["decision"] == "paused":
            if _pause_shadow_candidate_for_market(candidate, gate=market_gate, now=now):
                updated.append(_candidate_update_row(candidate, _candidate_stats(stats_by_candidate, candidate), reason="shadow_market_performance_paused"))
            skipped.append(
                {
                    "candidate_key": candidate.candidate_key,
                    "symbol": candidate.symbol,
                    "direction": candidate.direction,
                    "reason": "shadow_market_performance_paused",
                    "market_gate": market_gate,
                }
            )
            continue
        stats = _candidate_stats(stats_by_candidate, candidate)
        if _update_shadow_lifecycle(candidate, stats, now=now):
            updated.append(
                {
                    "candidate_key": candidate.candidate_key,
                    "shadow_status": candidate.shadow_status,
                    "promotion_status": candidate.promotion_status,
                    "closed_trades": stats["closed_trades"],
                    "open_trades": stats["open_trades"],
                }
            )
        if candidate.shadow_status in {"passed", "failed"}:
            skipped.append({"candidate_key": candidate.candidate_key, "reason": f"shadow_{candidate.shadow_status}"})
            continue
        if stats["open_trades"]:
            skipped.append({"candidate_key": candidate.candidate_key, "reason": "open_shadow_forward_exists"})
            continue
        market_open_count = open_market_counts.get((candidate.symbol, candidate.direction), 0)
        if market_open_count >= DEFAULT_MAX_OPEN_SHADOW_TRADES_PER_MARKET:
            skipped.append(
                {
                    "candidate_key": candidate.candidate_key,
                    "symbol": candidate.symbol,
                    "direction": candidate.direction,
                    "reason": "market_shadow_forward_slot_full",
                    "open_market_trades": market_open_count,
                    "max_open_market_trades": DEFAULT_MAX_OPEN_SHADOW_TRADES_PER_MARKET,
                }
            )
            continue
        duplicate_plan = _open_duplicate_shadow_plan_from_index(open_plan_index, candidate)
        if duplicate_plan is not None:
            if _retire_shadow_candidate(
                candidate,
                reason="duplicate_shadow_forward_plan",
                entry_plan_state=None,
                now=now,
                blocker=PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN,
            ):
                updated.append(_candidate_update_row(candidate, stats, reason="duplicate_shadow_forward_plan"))
            skipped.append(
                {
                    "candidate_key": candidate.candidate_key,
                    "symbol": candidate.symbol,
                    "reason": "duplicate_shadow_forward_plan",
                    "existing_candidate_key": duplicate_plan.candidate_key,
                    "existing_trade_id": duplicate_plan.id,
                }
            )
            continue
        plan_check = _candidate_plan_openable(
            candidate,
            now=now,
            max_candidate_age_hours=max_candidate_age_hours,
        )
        if plan_check is not None:
            if plan_check in _TERMINAL_PLAN_CHECK_REASONS:
                if _retire_shadow_candidate(candidate, reason=plan_check, entry_plan_state=None, now=now):
                    updated.append(_candidate_update_row(candidate, stats, reason=plan_check))
            skipped.append({"candidate_key": candidate.candidate_key, "reason": plan_check})
            continue
        price = latest_prices.get(candidate.symbol)
        if price is None or price <= 0:
            skipped.append({"candidate_key": candidate.candidate_key, "symbol": candidate.symbol, "reason": "missing_latest_price"})
            continue
        entry_plan_state = _candidate_entry_plan_state(
            candidate,
            mark_price=price,
            now=now,
            entry_tolerance_pct=entry_tolerance_pct,
        )
        if entry_plan_state["state"] != "triggered":
            if entry_plan_state["state"] in _TERMINAL_ENTRY_PLAN_STATES:
                reason = f"entry_plan_{entry_plan_state['state']}"
                if _retire_shadow_candidate(candidate, reason=reason, entry_plan_state=entry_plan_state, now=now):
                    updated.append(_candidate_update_row(candidate, stats, reason=reason))
            skipped.append(
                {
                    "candidate_key": candidate.candidate_key,
                    "symbol": candidate.symbol,
                    "reason": f"entry_plan_{entry_plan_state['state']}",
                    "mark_price": price,
                    "planned_entry": candidate.entry_price,
                    "entry_plan_state": entry_plan_state,
                }
            )
            continue
        trade = _shadow_trade_from_candidate(
            candidate,
            mark_price=price,
            now=now,
            entry_tolerance_pct=entry_tolerance_pct,
            entry_plan_state=entry_plan_state,
        )
        if trade is None:
            skipped.append({"candidate_key": candidate.candidate_key, "reason": "invalid_shadow_trade_shape"})
            continue
        if alpha_exploration_group_key is not None:
            _mark_paused_alpha_group_exploration(
                candidate,
                trade=trade,
                group_key=alpha_exploration_group_key,
                now=now,
            )
            paused_group_exploration_budget[alpha_exploration_group_key] -= 1
        session.add(trade)
        _index_open_shadow_trade(open_plan_index, trade)
        open_market_counts[(candidate.symbol, candidate.direction)] = open_market_counts.get((candidate.symbol, candidate.direction), 0) + 1
        candidate.shadow_status = "collecting"
        candidate.updated_at = now
        _apply_lifecycle(candidate)
        opened.append(
            {
                "candidate_key": candidate.candidate_key,
                "symbol": candidate.symbol,
                "direction": candidate.direction,
                "entry_price": trade.entry_price,
                "stop_loss": trade.stop_loss,
                "take_profit": trade.take_profit,
                "entry_plan_state": entry_plan_state,
            }
        )
        if len(opened) >= requested_limit:
            break
    if opened or updated:
        await session.commit()
    skip_reason_counts: dict[str, int] = {}
    for item in skipped:
        reason = str(item.get("reason") or "unknown")
        skip_reason_counts[reason] = skip_reason_counts.get(reason, 0) + 1
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "requested_limit": requested_limit,
        "scan_limit": scan_limit,
        "scanned": scanned,
        "opened_count": len(opened),
        "updated_count": len(updated),
        "skipped_count": len(skipped),
        "skip_reason_counts": skip_reason_counts,
        "opened": opened,
        "updated": updated[:100],
        "skipped": skipped[:100],
        "thresholds": {
            "max_candidate_age_hours": float(max_candidate_age_hours),
            "entry_tolerance_pct": float(entry_tolerance_pct),
            "paused_group_exploration_limit": max(0, int(paused_group_exploration_limit)),
        },
        "alpha_sampling": {
            "applied": bool(priority_groups or paused_groups),
            "priority_group_count": len(priority_groups),
            "paused_group_count": len(paused_groups),
            "paused_group_exploration_limit": max(0, int(paused_group_exploration_limit)),
        },
        "policy": _policy(),
    }


async def refresh_darkflow_candidate_promotion(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_PROMOTION_LIMIT,
    shadow_limit: int = DEFAULT_SHADOW_FORWARD_LIMIT,
    max_candidate_age_hours: float = DEFAULT_MAX_CANDIDATE_AGE_HOURS,
    entry_tolerance_pct: float = DEFAULT_ENTRY_TOLERANCE_PCT,
    materialize: bool = True,
    priority_group_keys: list[str] | set[str] | tuple[str, ...] | None = None,
    paused_group_keys: list[str] | set[str] | tuple[str, ...] | None = None,
    paused_group_exploration_limit: int = DEFAULT_PAUSED_GROUP_EXPLORATION_LIMIT,
) -> dict[str, Any]:
    materialize_result: dict[str, Any] = {"enabled": False}
    if materialize:
        materialize_result = await materialize_darkflow_trade_candidates(session, limit=limit)
    audit = await audit_darkflow_trade_candidates(session, limit=limit, include_blocked=True)
    shadow = await open_darkflow_shadow_forward_samples(
        session,
        limit=shadow_limit,
        max_candidate_age_hours=max_candidate_age_hours,
        entry_tolerance_pct=entry_tolerance_pct,
        priority_group_keys=priority_group_keys,
        paused_group_keys=paused_group_keys,
        paused_group_exploration_limit=paused_group_exploration_limit,
    )
    summary = await darkflow_candidate_promotion_report(session, limit=limit)
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "materialize": materialize_result,
        "audit": audit,
        "shadow_forward": shadow,
        "summary": summary,
        "policy": _policy(),
    }


async def darkflow_candidate_promotion_report(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_PROMOTION_LIMIT,
) -> dict[str, Any]:
    rows = await _candidate_rows(session, limit=limit, include_blocked=True)
    now = utc_now()
    latest_prices = await _latest_prices(session, sorted({candidate.symbol for candidate in rows}))
    stats_by_candidate = await _shadow_stats_by_candidate(session, [candidate.candidate_key for candidate in rows])
    counts: dict[str, int] = {}
    anti_counts: dict[str, int] = {}
    shadow_counts: dict[str, int] = {}
    gate_counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    gate_samples: list[dict[str, Any]] = []
    for candidate in rows:
        counts[candidate.promotion_status] = counts.get(candidate.promotion_status, 0) + 1
        anti_counts[candidate.anti_repaint_status] = anti_counts.get(candidate.anti_repaint_status, 0) + 1
        shadow_counts[candidate.shadow_status] = shadow_counts.get(candidate.shadow_status, 0) + 1
        stats = _candidate_stats(stats_by_candidate, candidate)
        price = latest_prices.get(candidate.symbol)
        if price is None or price <= 0:
            entry_plan_state = _missing_price_entry_plan_state(candidate, now=now)
        else:
            entry_plan_state = _candidate_entry_plan_state(
                candidate,
                mark_price=price,
                now=now,
                entry_tolerance_pct=DEFAULT_ENTRY_TOLERANCE_PCT,
            )
        gate = _promotion_gate_report_row(candidate, entry_plan_state=entry_plan_state, shadow_stats=stats)
        gate_counts[gate["gate_status"]] = gate_counts.get(gate["gate_status"], 0) + 1
        if len(samples) < 25:
            samples.append(
                {
                    "candidate_key": candidate.candidate_key,
                    "symbol": candidate.symbol,
                    "direction": candidate.direction,
                    "status": candidate.status,
                    "promotion_status": candidate.promotion_status,
                    "anti_repaint_status": candidate.anti_repaint_status,
                    "shadow_status": candidate.shadow_status,
                    "promotion_blockers": candidate.promotion_blockers,
                    "entry_plan_state": entry_plan_state,
                    "shadow_stats": stats,
                    "gate_status": gate["gate_status"],
                    "primary_gate_blocker": gate["primary_blocker"],
                    "promotion_gate": gate,
                }
            )
        if len(gate_samples) < 25:
            gate_samples.append(gate)
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "candidate_count": len(rows),
        "promotion_status_counts": counts,
        "anti_repaint_status_counts": anti_counts,
        "shadow_status_counts": shadow_counts,
        "gate_status_counts": gate_counts,
        "samples": samples,
        "gate_samples": gate_samples,
        "criteria": _shadow_criteria(),
        "policy": _policy() | promotion_gate_policy(),
    }


def _promotion_gate_report_row(
    candidate: TradeCandidate,
    *,
    entry_plan_state: dict[str, Any],
    shadow_stats: dict[str, Any],
) -> dict[str, Any]:
    decision = decide_promotion_gate(
        PromotionGateEvidence(
            candidate_key=candidate.candidate_key,
            lineage=candidate.lineage,
            status=candidate.status,
            promotion_status=candidate.promotion_status,
            anti_repaint_status=candidate.anti_repaint_status,
            shadow_status=candidate.shadow_status,
            promotion_blockers=tuple(_candidate_gate_blockers(candidate)),
            entry_plan_state=entry_plan_state,
            shadow_stats=shadow_stats,
        )
    )
    return {
        "candidate_key": decision.candidate_key,
        "symbol": candidate.symbol,
        "direction": candidate.direction,
        "status": candidate.status,
        "promotion_status": candidate.promotion_status,
        "anti_repaint_status": candidate.anti_repaint_status,
        "shadow_status": candidate.shadow_status,
        "gate_status": decision.gate_status,
        "primary_blocker": decision.primary_blocker,
        "next_action": decision.next_action,
        "blocker_groups": decision.blocker_groups,
        "raw_blockers": decision.raw_blockers,
        "evidence_summary": decision.evidence_summary,
    }


def _candidate_gate_blockers(candidate: TradeCandidate) -> list[str]:
    blockers = [str(item) for item in (candidate.promotion_blockers or [])]
    blockers.extend(str(item) for item in (candidate.blockers or []))
    return list(dict.fromkeys(blockers))


async def darkflow_entry_plan_state_report(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_PROMOTION_LIMIT,
    entry_tolerance_pct: float = DEFAULT_ENTRY_TOLERANCE_PCT,
) -> dict[str, Any]:
    rows = await _candidate_rows(session, limit=limit, include_blocked=True)
    now = utc_now()
    prices = await _latest_prices(session, sorted({candidate.symbol for candidate in rows}))
    counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    missing_price_count = 0
    samples: list[dict[str, Any]] = []
    for candidate in rows:
        price = prices.get(candidate.symbol)
        if price is None or price <= 0:
            state = _missing_price_entry_plan_state(candidate, now=now)
            missing_price_count += 1
        else:
            state = _candidate_entry_plan_state(
                candidate,
                mark_price=price,
                now=now,
                entry_tolerance_pct=entry_tolerance_pct,
            )
        state_key = str(state.get("state") or "unknown")
        reason_key = str(state.get("reason") or "unknown")
        counts[state_key] = counts.get(state_key, 0) + 1
        reason_counts[reason_key] = reason_counts.get(reason_key, 0) + 1
        if len(samples) < 25:
            samples.append(
                {
                    "candidate_key": candidate.candidate_key,
                    "symbol": candidate.symbol,
                    "direction": candidate.direction,
                    "status": candidate.status,
                    "promotion_status": candidate.promotion_status,
                    "anti_repaint_status": candidate.anti_repaint_status,
                    "shadow_status": candidate.shadow_status,
                    "entry_price": candidate.entry_price,
                    "stop_price": candidate.stop_price,
                    "target_price": candidate.target_price,
                    "quality_score": candidate.quality_score,
                    "entry_plan_state": state,
                }
            )
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "requested_limit": max(1, int(limit)),
        "candidate_count": len(rows),
        "generated_at": _iso(now),
        "freshness": await _darkflow_freshness(session, now=now, rows=rows),
        "state_counts": counts,
        "reason_counts": reason_counts,
        "missing_price_count": missing_price_count,
        "samples": samples,
        "thresholds": {"entry_tolerance_pct": float(entry_tolerance_pct)},
        "policy": _policy() | {
            "opens_paper_trades": False,
            "opens_live_orders": False,
            "mutates_candidate_state": False,
            "report_only": True,
        },
    }


async def _darkflow_freshness(
    session: AsyncSession,
    *,
    now: datetime,
    rows: list[TradeCandidate],
) -> dict[str, Any]:
    latest_price_at = await session.scalar(select(PriceSnapshot.created_at).order_by(PriceSnapshot.created_at.desc()).limit(1))
    latest_snapshot_at = await session.scalar(select(SignalSnapshot.created_at).order_by(SignalSnapshot.created_at.desc()).limit(1))
    latest_interaction_event_at = await session.scalar(
        select(DarkflowInteraction.event_ts).order_by(DarkflowInteraction.event_ts.desc(), DarkflowInteraction.id.desc()).limit(1)
    )
    latest_interaction_created_at = await session.scalar(
        select(DarkflowInteraction.created_at).order_by(DarkflowInteraction.created_at.desc(), DarkflowInteraction.id.desc()).limit(1)
    )
    latest_pipeline_run_at = await session.scalar(
        select(ExperimentRun.created_at)
        .where(ExperimentRun.name == "darkflow_pipeline_run", ExperimentRun.status.in_(["running", "research"]))
        .order_by(ExperimentRun.created_at.desc(), ExperimentRun.id.desc())
        .limit(1)
    )
    latest_candidate_setup_at = max((_aware(item.setup_time) for item in rows if item.setup_time is not None), default=None)
    latest_candidate_updated_at = await session.scalar(
        select(TradeCandidate.updated_at)
        .where(TradeCandidate.lineage == "core_darkflow_v2")
        .order_by(TradeCandidate.updated_at.desc(), TradeCandidate.id.desc())
        .limit(1)
    )
    latest_candidate_updated_at = _aware(latest_candidate_updated_at)
    candidate_age_minutes = _age_minutes(now, latest_candidate_setup_at)
    interaction_age_minutes = _age_minutes(now, latest_interaction_event_at)
    interaction_pipeline_age_minutes = _freshest_age_minutes(now, latest_pipeline_run_at, latest_interaction_created_at)
    candidate_pipeline_age_minutes = _freshest_age_minutes(now, latest_pipeline_run_at, latest_candidate_updated_at)
    snapshot_age_minutes = _age_minutes(now, latest_snapshot_at)
    price_age_minutes = _age_minutes(now, latest_price_at)
    stale_reasons: list[str] = []
    if price_age_minutes is None or price_age_minutes > 60:
        stale_reasons.append("latest_price_stale")
    if snapshot_age_minutes is None or snapshot_age_minutes > 60:
        stale_reasons.append("latest_signal_snapshot_stale")
    if interaction_pipeline_age_minutes is None or interaction_pipeline_age_minutes > 30:
        stale_reasons.append("darkflow_pipeline_not_running")
    if candidate_pipeline_age_minutes is None or candidate_pipeline_age_minutes > 30:
        stale_reasons.append("trade_candidate_pipeline_not_running")
    opportunity_reasons: list[str] = []
    if interaction_age_minutes is None or interaction_age_minutes > 90:
        opportunity_reasons.append("no_recent_darkflow_interaction_opportunity")
    if candidate_age_minutes is None or candidate_age_minutes > 90:
        opportunity_reasons.append("no_recent_trade_candidate_setup")
    return {
        "status": "stale" if stale_reasons else "fresh",
        "stale_reasons": stale_reasons,
        "opportunity_status": "quiet" if opportunity_reasons else "active",
        "opportunity_reasons": opportunity_reasons,
        "latest_price_at": _iso(_aware(latest_price_at)),
        "latest_signal_snapshot_at": _iso(_aware(latest_snapshot_at)),
        "latest_interaction_event_at": _iso(latest_interaction_event_at),
        "latest_interaction_created_at": _iso(_aware(latest_interaction_created_at)),
        "latest_pipeline_run_at": _iso(_aware(latest_pipeline_run_at)),
        "latest_candidate_setup_at": _iso(latest_candidate_setup_at),
        "latest_candidate_updated_at": _iso(latest_candidate_updated_at),
        "age_minutes": {
            "price": price_age_minutes,
            "signal_snapshot": snapshot_age_minutes,
            "interaction_event": interaction_age_minutes,
            "interaction_pipeline": interaction_pipeline_age_minutes,
            "candidate_setup": candidate_age_minutes,
            "candidate_pipeline": candidate_pipeline_age_minutes,
        },
        "pipeline": {
            "expected_worker": "darkflow-worker",
            "expected_command": "python -m app.cli darkflow-loop --interval-seconds 120 --backtest-every-runs 5",
            "report_only": True,
        },
    }


async def _candidate_rows(
    session: AsyncSession,
    *,
    limit: int,
    include_blocked: bool,
    prioritize_pending_shadow_audit: bool = False,
) -> list[TradeCandidate]:
    query = select(TradeCandidate).where(TradeCandidate.lineage == "core_darkflow_v2")
    if not include_blocked:
        query = query.where(TradeCandidate.status == "shadow_candidate")
    order_by = []
    if prioritize_pending_shadow_audit:
        order_by.append(
            case(
                (
                    (TradeCandidate.status == "shadow_candidate")
                    & (TradeCandidate.anti_repaint_status != "passed")
                    & (TradeCandidate.shadow_status.in_(["not_started", "collecting"])),
                    0,
                ),
                (TradeCandidate.status == "shadow_candidate", 1),
                else_=2,
            )
        )
    order_by.extend([TradeCandidate.setup_time.desc(), TradeCandidate.updated_at.desc(), TradeCandidate.id.desc()])
    rows = await session.scalars(
        query.order_by(*order_by).limit(max(1, int(limit)))
    )
    return list(rows.all())


def _shadow_candidate_scan_limit(limit: int) -> int:
    requested = max(1, int(limit))
    return min(max(requested * DEFAULT_SHADOW_FORWARD_SCAN_MULTIPLIER, requested + 100), 1000)


async def _shadow_ready_candidate_rows(session: AsyncSession, *, limit: int) -> list[TradeCandidate]:
    rows = await session.scalars(
        select(TradeCandidate)
        .where(
            TradeCandidate.lineage == "core_darkflow_v2",
            TradeCandidate.status == "shadow_candidate",
            TradeCandidate.anti_repaint_status == "passed",
            TradeCandidate.shadow_status.in_(["not_started", "collecting"]),
        )
        .order_by(
            case((TradeCandidate.shadow_status == "not_started", 0), else_=1),
            TradeCandidate.setup_time.desc(),
            TradeCandidate.updated_at.desc(),
            TradeCandidate.id.desc(),
        )
        .limit(max(1, int(limit)))
    )
    candidates = list(rows.all())
    market_stats = await _shadow_market_performance_stats(session)
    stats_by_candidate = await _shadow_stats_by_candidate(session, [candidate.candidate_key for candidate in candidates])

    def sort_key(candidate: TradeCandidate) -> tuple[int, int, datetime, datetime, str]:
        gate = _shadow_market_gate_from_stats(candidate, market_stats)
        rank = {"priority": 2, "neutral": 1, "paused": 0}.get(str(gate["decision"]), 1)
        stats = _candidate_stats(stats_by_candidate, candidate)
        no_shadow_sample_rank = 1 if stats["total_trades"] == 0 else 0
        return (
            rank,
            no_shadow_sample_rank,
            _aware(candidate.setup_time or datetime.min.replace(tzinfo=timezone.utc)),
            _aware(candidate.updated_at or datetime.min.replace(tzinfo=timezone.utc)),
            candidate.id,
        )

    ranked = sorted(candidates, key=sort_key, reverse=True)
    return _round_robin_candidates_by_market(ranked)


def _round_robin_candidates_by_market(candidates: list[TradeCandidate]) -> list[TradeCandidate]:
    buckets: dict[tuple[str, str], list[TradeCandidate]] = {}
    market_order: list[tuple[str, str]] = []
    for candidate in candidates:
        key = (candidate.symbol, candidate.direction)
        if key not in buckets:
            buckets[key] = []
            market_order.append(key)
        buckets[key].append(candidate)
    result: list[TradeCandidate] = []
    while buckets:
        for key in list(market_order):
            bucket = buckets.get(key)
            if not bucket:
                buckets.pop(key, None)
                market_order.remove(key)
                continue
            result.append(bucket.pop(0))
            if not bucket:
                buckets.pop(key, None)
                market_order.remove(key)
    return result


async def _source_interaction(session: AsyncSession, candidate: TradeCandidate) -> DarkflowInteraction | None:
    if candidate.source_interaction_id:
        row = await session.get(DarkflowInteraction, candidate.source_interaction_id)
        if row is not None:
            return row
    payload = candidate.decision_payload or {}
    source_id = payload.get("source_interaction_id")
    if isinstance(source_id, str) and source_id:
        return await session.get(DarkflowInteraction, source_id)
    return None


def _anti_repaint_audit(candidate: TradeCandidate, source: DarkflowInteraction | None) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    failures: list[str] = []
    if source is None:
        return {"passed": False, "checks": {"source_interaction_exists": False}, "failures": ["source_interaction_missing"]}
    card = decision_card_from_interaction(source)
    checks["source_interaction_exists"] = True
    checks["decision_card_rebuilds"] = card is not None
    if card is None:
        failures.append("decision_card_rebuild_failed")
        return {"passed": False, "checks": checks, "failures": failures}
    checks["candidate_key_stable"] = card["card_id"] == candidate.candidate_key
    checks["source_id_stable"] = str(card.get("source_interaction_id") or "") == str(candidate.source_interaction_id or "")
    entry_plan = card["entry_plan"]
    target = entry_plan["take_profit_levels"][0]
    checks["entry_stable"] = _close(entry_plan["planned_entry"], candidate.entry_price)
    checks["stop_stable"] = _close(entry_plan["planned_stop"], candidate.stop_price)
    checks["target_stable"] = _close(target["price"], candidate.target_price)
    checks["direction_stable"] = str(card["direction"]) == candidate.direction
    checks["strategy_stable"] = str(card["strategy_id"]) == candidate.strategy_id
    checks["research_only"] = bool((card.get("context") or {}).get("research_only")) is True
    checks["no_execution_flags"] = card["risk_gate"].get("paper_eligible") is False and card["risk_gate"].get("live_eligible") is False
    for key, passed in checks.items():
        if not passed:
            failures.append(key)
    return {"passed": not failures, "checks": checks, "failures": failures}


async def _latest_price(session: AsyncSession, symbol: str) -> float | None:
    row = await session.scalar(
        select(PriceSnapshot.price).where(PriceSnapshot.symbol == symbol).order_by(PriceSnapshot.created_at.desc()).limit(1)
    )
    return float(row) if isinstance(row, (int, float)) else None


async def _latest_prices(session: AsyncSession, symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    ranked = (
        select(
            PriceSnapshot.id.label("id"),
            func.row_number()
            .over(partition_by=PriceSnapshot.symbol, order_by=PriceSnapshot.created_at.desc())
            .label("rn"),
        )
        .where(PriceSnapshot.symbol.in_(symbols))
        .subquery()
    )
    rows = await session.execute(
        select(PriceSnapshot.symbol, PriceSnapshot.price)
        .join(ranked, PriceSnapshot.id == ranked.c.id)
        .where(ranked.c.rn == 1)
    )
    return {symbol: float(price) for symbol, price in rows.all() if isinstance(price, (int, float)) and float(price) > 0}


async def _shadow_stats(session: AsyncSession, candidate_key: str) -> dict[str, Any]:
    rows = await session.scalars(
        select(ShadowPaperTrade)
        .where(
            ShadowPaperTrade.strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME,
            ShadowPaperTrade.candidate_key == candidate_key,
        )
        .order_by(ShadowPaperTrade.opened_at)
    )
    trades = list(rows.all())
    closed = [item for item in trades if item.status == "closed" and isinstance(item.pnl, (int, float))]
    wins = [float(item.pnl) for item in closed if float(item.pnl or 0.0) > 0]
    losses = [float(item.pnl) for item in closed if float(item.pnl or 0.0) < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    returns = [float(item.pnl or 0.0) for item in closed]
    return {
        "open_trades": sum(1 for item in trades if item.status == "open"),
        "closed_trades": len(closed),
        "total_trades": len(trades),
        "win_rate": len(wins) / len(closed) if closed else None,
        "avg_pnl": mean(returns) if returns else None,
        "profit_factor": gross_win / gross_loss if gross_loss else (999.0 if gross_win else None),
        "max_drawdown": _max_drawdown(returns),
    }


async def _shadow_stats_by_candidate(session: AsyncSession, candidate_keys: list[str]) -> dict[str, dict[str, Any]]:
    keys = [key for key in dict.fromkeys(candidate_keys) if key]
    if not keys:
        return {}
    rows = await session.scalars(
        select(ShadowPaperTrade)
        .where(
            ShadowPaperTrade.strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME,
            ShadowPaperTrade.candidate_key.in_(keys),
        )
        .order_by(ShadowPaperTrade.opened_at)
    )
    buckets: dict[str, list[ShadowPaperTrade]] = {key: [] for key in keys}
    for trade in rows.all():
        buckets.setdefault(trade.candidate_key, []).append(trade)
    return {key: _candidate_trade_stats(items) for key, items in buckets.items()}


def _candidate_stats(stats_by_candidate: dict[str, dict[str, Any]], candidate: TradeCandidate) -> dict[str, Any]:
    return stats_by_candidate.get(candidate.candidate_key) or _empty_candidate_stats()


def _candidate_trade_stats(trades: list[ShadowPaperTrade]) -> dict[str, Any]:
    closed = [item for item in trades if item.status == "closed" and isinstance(item.pnl, (int, float))]
    wins = [float(item.pnl) for item in closed if float(item.pnl or 0.0) > 0]
    losses = [float(item.pnl) for item in closed if float(item.pnl or 0.0) < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    returns = [float(item.pnl or 0.0) for item in closed]
    return {
        "open_trades": sum(1 for item in trades if item.status == "open"),
        "closed_trades": len(closed),
        "total_trades": len(trades),
        "win_rate": len(wins) / len(closed) if closed else None,
        "avg_pnl": mean(returns) if returns else None,
        "profit_factor": gross_win / gross_loss if gross_loss else (999.0 if gross_win else None),
        "max_drawdown": _max_drawdown(returns),
    }


def _empty_candidate_stats() -> dict[str, Any]:
    return {
        "open_trades": 0,
        "closed_trades": 0,
        "total_trades": 0,
        "win_rate": None,
        "avg_pnl": None,
        "profit_factor": None,
        "max_drawdown": 0.0,
    }


async def _shadow_market_performance_gate(session: AsyncSession, candidate: TradeCandidate) -> dict[str, Any]:
    return _shadow_market_gate_from_stats(candidate, await _shadow_market_performance_stats(session))


async def _shadow_market_performance_stats(session: AsyncSession) -> dict[tuple[str, str], dict[str, Any]]:
    rows = await session.scalars(
        select(ShadowPaperTrade)
        .where(ShadowPaperTrade.strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME)
        .order_by(ShadowPaperTrade.opened_at)
    )
    buckets: dict[tuple[str, str], list[ShadowPaperTrade]] = {}
    for trade in rows.all():
        buckets.setdefault((trade.symbol, trade.direction), []).append(trade)
    return {key: _market_trade_stats(_unique_market_plan_trades(items)) for key, items in buckets.items()}


def _shadow_market_gate_from_stats(candidate: TradeCandidate, stats_by_market: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    stats = stats_by_market.get((candidate.symbol, candidate.direction)) or _empty_market_stats()
    closed = int(stats.get("closed_trades") or 0)
    win_rate = stats.get("win_rate")
    profit_factor = stats.get("profit_factor")
    if (
        closed >= SHADOW_MARKET_MIN_CLOSED_TRADES
        and isinstance(win_rate, (int, float))
        and isinstance(profit_factor, (int, float))
        and float(win_rate) <= SHADOW_MARKET_PAUSE_MAX_WIN_RATE
        and float(profit_factor) <= SHADOW_MARKET_PAUSE_MAX_PROFIT_FACTOR
    ):
        decision = "paused"
        reason = "weak_symbol_direction_shadow_performance"
    elif (
        closed >= SHADOW_MARKET_MIN_CLOSED_TRADES
        and isinstance(win_rate, (int, float))
        and isinstance(profit_factor, (int, float))
        and float(win_rate) >= SHADOW_MARKET_PRIORITY_MIN_WIN_RATE
        and float(profit_factor) >= SHADOW_MARKET_PRIORITY_MIN_PROFIT_FACTOR
    ):
        decision = "priority"
        reason = "strong_symbol_direction_shadow_performance"
    else:
        decision = "neutral"
        reason = "insufficient_or_mixed_symbol_direction_shadow_performance"
    return {
        "decision": decision,
        "reason": reason,
        "symbol": candidate.symbol,
        "direction": candidate.direction,
        "closed_trades": closed,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_drawdown": stats.get("max_drawdown"),
        "thresholds": {
            "min_closed_trades": SHADOW_MARKET_MIN_CLOSED_TRADES,
            "pause_max_win_rate": SHADOW_MARKET_PAUSE_MAX_WIN_RATE,
            "pause_max_profit_factor": SHADOW_MARKET_PAUSE_MAX_PROFIT_FACTOR,
            "priority_min_win_rate": SHADOW_MARKET_PRIORITY_MIN_WIN_RATE,
            "priority_min_profit_factor": SHADOW_MARKET_PRIORITY_MIN_PROFIT_FACTOR,
        },
    }


def _pause_shadow_candidate_for_market(candidate: TradeCandidate, *, gate: dict[str, Any], now: datetime) -> bool:
    previous = (
        candidate.status,
        candidate.shadow_status,
        candidate.promotion_status,
        tuple(candidate.promotion_blockers or []),
        candidate.decision_payload,
    )
    blockers = set(str(item) for item in (candidate.promotion_blockers or []))
    blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
    blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING)
    blockers.add(PROMOTION_BLOCKER_SHADOW_MARKET_PAUSED)
    candidate.promotion_blockers = _ordered_blockers(blockers)
    candidate.shadow_status = "retired"
    candidate.status = "entry_plan_retired"
    payload = dict(candidate.decision_payload or {})
    payload["shadow_market_gate"] = gate | {"paused_at": _iso(now)}
    candidate.decision_payload = payload
    candidate.updated_at = now
    _apply_lifecycle(candidate)
    return previous != (
        candidate.status,
        candidate.shadow_status,
        candidate.promotion_status,
        tuple(candidate.promotion_blockers or []),
        candidate.decision_payload,
    )


def _pause_shadow_candidate_for_alpha_group(candidate: TradeCandidate, *, group_key: str, now: datetime) -> bool:
    previous = (
        candidate.status,
        candidate.shadow_status,
        candidate.promotion_status,
        tuple(candidate.promotion_blockers or []),
        candidate.decision_payload,
    )
    blockers = set(str(item) for item in (candidate.promotion_blockers or []))
    blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
    blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING)
    blockers.add(PROMOTION_BLOCKER_SHADOW_MARKET_PAUSED)
    candidate.promotion_blockers = _ordered_blockers(blockers)
    candidate.shadow_status = "retired"
    candidate.status = "entry_plan_retired"
    payload = dict(candidate.decision_payload or {})
    payload["alpha_sampling_gate"] = {
        "decision": "paused",
        "reason": "weak_alpha_group_shadow_performance",
        "group_key": group_key,
        "paused_at": _iso(now),
    }
    candidate.decision_payload = payload
    candidate.updated_at = now
    _apply_lifecycle(candidate)
    return previous != (
        candidate.status,
        candidate.shadow_status,
        candidate.promotion_status,
        tuple(candidate.promotion_blockers or []),
        candidate.decision_payload,
    )


def _mark_paused_alpha_group_exploration(
    candidate: TradeCandidate,
    *,
    trade: ShadowPaperTrade,
    group_key: str,
    now: datetime,
) -> None:
    gate = {
        "decision": "exploration",
        "reason": "limited_paused_group_probe",
        "group_key": group_key,
        "opened_at": _iso(now),
    }
    payload = dict(candidate.decision_payload or {})
    payload["alpha_sampling_gate"] = gate
    candidate.decision_payload = payload
    context = dict(trade.context or {})
    context["alpha_sampling_gate"] = gate
    trade.context = context
    candidate.updated_at = now


def _normalized_group_keys(values: list[str] | set[str] | tuple[str, ...] | None) -> set[str]:
    return {str(item) for item in (values or []) if str(item)}


def _paused_group_exploration_budget(paused_groups: set[str], limit: int) -> dict[str, int]:
    budget = max(0, int(limit))
    return {group_key: budget for group_key in paused_groups}


def _sort_candidates_by_alpha_sampling_plan(
    candidates: list[TradeCandidate],
    *,
    priority_groups: set[str],
    paused_groups: set[str],
) -> list[TradeCandidate]:
    def rank(candidate: TradeCandidate) -> tuple[int, datetime, datetime, str]:
        group_key = _candidate_alpha_group_key(candidate)
        if group_key in priority_groups:
            group_rank = 2
        elif group_key in paused_groups:
            group_rank = 0
        else:
            group_rank = 1
        return (
            group_rank,
            _aware(candidate.setup_time or datetime.min.replace(tzinfo=timezone.utc)),
            _aware(candidate.updated_at or datetime.min.replace(tzinfo=timezone.utc)),
            candidate.id,
        )

    return sorted(candidates, key=rank, reverse=True)


def _candidate_alpha_group_key(candidate: TradeCandidate) -> str:
    return "|".join(
        [
            candidate.strategy_id,
            candidate.symbol,
            candidate.direction,
            candidate.timeframe,
            candidate.market_state,
        ]
    )


def _unique_market_plan_trades(trades: list[ShadowPaperTrade]) -> list[ShadowPaperTrade]:
    best_by_plan: dict[str, ShadowPaperTrade] = {}
    for trade in trades:
        key = _trade_market_plan_fingerprint(trade)
        current = best_by_plan.get(key)
        if current is None or _trade_market_plan_rank(trade) > _trade_market_plan_rank(current):
            best_by_plan[key] = trade
    return list(best_by_plan.values())


def _trade_market_plan_rank(trade: ShadowPaperTrade) -> tuple[int, datetime, str]:
    closed_rank = 1 if trade.status == "closed" and isinstance(trade.pnl, (int, float)) else 0
    observed_at = trade.closed_at or trade.opened_at or datetime.min.replace(tzinfo=timezone.utc)
    return (closed_rank, _aware(observed_at), trade.id)


def _trade_market_plan_fingerprint(trade: ShadowPaperTrade) -> str:
    context = trade.context if isinstance(trade.context, dict) else {}
    explicit = context.get("shadow_plan_fingerprint")
    if explicit:
        return f"explicit:{explicit}"
    snapshot = context.get("candidate_snapshot") if isinstance(context.get("candidate_snapshot"), dict) else {}
    return ":".join(
        [
            trade.strategy_name,
            str(snapshot.get("strategy_id") or trade.strategy_name),
            trade.symbol,
            trade.timeframe,
            trade.direction,
            _rounded_price_bucket(_float(snapshot.get("entry_price")) or trade.entry_price),
            _rounded_price_bucket(_float(snapshot.get("stop_price")) or trade.stop_loss),
            _rounded_price_bucket(_float(snapshot.get("target_price")) or trade.take_profit),
        ]
    )


def _market_trade_stats(trades: list[ShadowPaperTrade]) -> dict[str, Any]:
    closed = sorted(
        [item for item in trades if item.status == "closed" and isinstance(item.pnl, (int, float))],
        key=lambda item: _aware(item.closed_at or item.opened_at or datetime.min.replace(tzinfo=timezone.utc)),
    )
    wins = [float(item.pnl) for item in closed if float(item.pnl or 0.0) > 0]
    losses = [float(item.pnl) for item in closed if float(item.pnl or 0.0) < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    returns = [float(item.pnl or 0.0) for item in closed]
    return {
        "open_trades": sum(1 for item in trades if item.status == "open"),
        "closed_trades": len(closed),
        "total_trades": len(trades),
        "win_rate": len(wins) / len(closed) if closed else None,
        "profit_factor": gross_win / gross_loss if gross_loss else (999.0 if gross_win else None),
        "max_drawdown": _max_drawdown(returns),
    }


def _empty_market_stats() -> dict[str, Any]:
    return {"open_trades": 0, "closed_trades": 0, "total_trades": 0, "win_rate": None, "profit_factor": None, "max_drawdown": 0.0}


async def _open_duplicate_shadow_plan(session: AsyncSession, candidate: TradeCandidate) -> ShadowPaperTrade | None:
    fingerprint = _candidate_plan_fingerprint(candidate)
    if fingerprint is None:
        return None
    rows = await session.scalars(
        select(ShadowPaperTrade)
        .where(
            ShadowPaperTrade.strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME,
            ShadowPaperTrade.status == "open",
            ShadowPaperTrade.symbol == candidate.symbol,
            ShadowPaperTrade.direction == candidate.direction,
        )
        .order_by(ShadowPaperTrade.opened_at.desc(), ShadowPaperTrade.id.desc())
        .limit(100)
    )
    for trade in rows.all():
        if trade.candidate_key == candidate.candidate_key:
            continue
        context = trade.context if isinstance(trade.context, dict) else {}
        existing = context.get("shadow_plan_fingerprint")
        if existing == fingerprint:
            return trade
        snapshot = context.get("candidate_snapshot") if isinstance(context.get("candidate_snapshot"), dict) else {}
        if _candidate_snapshot_matches_plan(candidate, snapshot):
            return trade
    return None


async def _open_shadow_plan_index(
    session: AsyncSession,
    candidates: list[TradeCandidate],
) -> dict[tuple[str, str], dict[str, ShadowPaperTrade]]:
    markets = sorted({(candidate.symbol, candidate.direction) for candidate in candidates})
    if not markets:
        return {}
    symbols = sorted({symbol for symbol, _direction in markets})
    directions = sorted({direction for _symbol, direction in markets})
    rows = await session.scalars(
        select(ShadowPaperTrade)
        .where(
            ShadowPaperTrade.strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME,
            ShadowPaperTrade.status == "open",
            ShadowPaperTrade.symbol.in_(symbols),
            ShadowPaperTrade.direction.in_(directions),
        )
        .order_by(ShadowPaperTrade.opened_at.desc(), ShadowPaperTrade.id.desc())
    )
    index: dict[tuple[str, str], dict[str, ShadowPaperTrade]] = {}
    for trade in rows.all():
        market = (trade.symbol, trade.direction)
        if market not in markets:
            continue
        bucket = index.setdefault(market, {})
        for key in _trade_open_plan_index_keys(trade):
            bucket.setdefault(key, trade)
    return index


def _open_duplicate_shadow_plan_from_index(
    index: dict[tuple[str, str], dict[str, ShadowPaperTrade]],
    candidate: TradeCandidate,
) -> ShadowPaperTrade | None:
    fingerprint = _candidate_plan_fingerprint(candidate)
    if fingerprint is None:
        return None
    return index.get((candidate.symbol, candidate.direction), {}).get(f"candidate:{fingerprint}")


def _open_shadow_market_counts(index: dict[tuple[str, str], dict[str, ShadowPaperTrade]]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for market, bucket in index.items():
        counts[market] = len({trade.id for trade in bucket.values()})
    return counts


def _index_open_shadow_trade(
    index: dict[tuple[str, str], dict[str, ShadowPaperTrade]],
    trade: ShadowPaperTrade,
) -> None:
    bucket = index.setdefault((trade.symbol, trade.direction), {})
    for key in _trade_open_plan_index_keys(trade):
        bucket[key] = trade


def _trade_open_plan_index_keys(trade: ShadowPaperTrade) -> list[str]:
    keys = {_trade_market_plan_fingerprint(trade)}
    candidate_key = _candidate_plan_fingerprint_from_trade(trade)
    if candidate_key:
        keys.add(f"candidate:{candidate_key}")
    return list(keys)


def _candidate_plan_fingerprint_from_trade(trade: ShadowPaperTrade) -> str | None:
    context = trade.context if isinstance(trade.context, dict) else {}
    snapshot = context.get("candidate_snapshot") if isinstance(context.get("candidate_snapshot"), dict) else {}
    strategy_id = str(snapshot.get("strategy_id") or "")
    timeframe = str(snapshot.get("timeframe") or trade.timeframe or "")
    entry = _float(snapshot.get("entry_price")) or float(trade.entry_price or 0.0)
    stop = _float(snapshot.get("stop_price")) or float(trade.stop_loss or 0.0)
    target = _float(snapshot.get("target_price")) or float(trade.take_profit or 0.0)
    if not strategy_id or entry <= 0 or stop <= 0 or target <= 0:
        return None
    return ":".join(
        [
            strategy_id,
            trade.symbol,
            timeframe,
            trade.direction,
            _rounded_price_bucket(entry),
            _rounded_price_bucket(stop),
            _rounded_price_bucket(target),
        ]
    )


def _candidate_plan_fingerprint(candidate: TradeCandidate) -> str | None:
    if candidate.entry_price <= 0 or candidate.stop_price <= 0 or candidate.target_price <= 0:
        return None
    entry = _rounded_price_bucket(candidate.entry_price)
    stop = _rounded_price_bucket(candidate.stop_price)
    target = _rounded_price_bucket(candidate.target_price)
    return ":".join(
        [
            candidate.strategy_id,
            candidate.symbol,
            candidate.timeframe,
            candidate.direction,
            entry,
            stop,
            target,
        ]
    )


def _candidate_snapshot_matches_plan(candidate: TradeCandidate, snapshot: dict[str, Any]) -> bool:
    if not snapshot:
        return False
    if str(snapshot.get("strategy_id") or "") != candidate.strategy_id:
        return False
    if str(snapshot.get("timeframe") or "") != candidate.timeframe:
        return False
    if _float(snapshot.get("entry_price")) is None or _float(snapshot.get("stop_price")) is None or _float(snapshot.get("target_price")) is None:
        return False
    return (
        _same_price_bucket(candidate.entry_price, float(snapshot["entry_price"]))
        and _same_price_bucket(candidate.stop_price, float(snapshot["stop_price"]))
        and _same_price_bucket(candidate.target_price, float(snapshot["target_price"]))
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


def _same_price_bucket(left: float, right: float) -> bool:
    if left <= 0 or right <= 0:
        return False
    return _rounded_price_bucket(left) == _rounded_price_bucket(right)


def _update_shadow_lifecycle(candidate: TradeCandidate, stats: dict[str, Any], *, now: datetime) -> bool:
    previous = (candidate.shadow_status, candidate.promotion_status, tuple(candidate.promotion_blockers or []))
    if candidate.shadow_status == "retired":
        candidate.updated_at = now
        _apply_lifecycle(candidate)
        return previous != (candidate.shadow_status, candidate.promotion_status, tuple(candidate.promotion_blockers or []))
    if stats["closed_trades"] >= SHADOW_FORWARD_MIN_CLOSED_TRADES:
        candidate.shadow_status = "passed" if _shadow_stats_pass(stats) else "failed"
    elif stats["open_trades"] or stats["total_trades"]:
        candidate.shadow_status = "collecting"
    else:
        candidate.shadow_status = "not_started"
    candidate.updated_at = now
    _apply_lifecycle(candidate)
    return previous != (candidate.shadow_status, candidate.promotion_status, tuple(candidate.promotion_blockers or []))


def _retire_shadow_candidate(
    candidate: TradeCandidate,
    *,
    reason: str,
    entry_plan_state: dict[str, Any] | None,
    now: datetime,
    blocker: str = PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED,
) -> bool:
    previous = (
        candidate.status,
        candidate.shadow_status,
        candidate.promotion_status,
        tuple(candidate.promotion_blockers or []),
        candidate.decision_payload,
    )
    candidate.status = "entry_plan_retired"
    candidate.shadow_status = "retired"
    blockers = set(str(item) for item in (candidate.promotion_blockers or []))
    blockers.add(blocker)
    if blocker == PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN:
        blockers.discard(PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED)
    candidate.promotion_blockers = _ordered_blockers(blockers)
    payload = dict(candidate.decision_payload or {})
    payload["entry_plan_retirement"] = {
        "reason": reason,
        "retired_at": _iso(now),
        "entry_plan_state": entry_plan_state or {},
    }
    candidate.decision_payload = payload
    candidate.updated_at = now
    _apply_lifecycle(candidate)
    return previous != (
        candidate.status,
        candidate.shadow_status,
        candidate.promotion_status,
        tuple(candidate.promotion_blockers or []),
        candidate.decision_payload,
    )


def _candidate_update_row(candidate: TradeCandidate, stats: dict[str, Any], *, reason: str | None = None) -> dict[str, Any]:
    row = {
        "candidate_key": candidate.candidate_key,
        "shadow_status": candidate.shadow_status,
        "promotion_status": candidate.promotion_status,
        "closed_trades": stats["closed_trades"],
        "open_trades": stats["open_trades"],
    }
    if reason:
        row["reason"] = reason
    return row


def _shadow_stats_pass(stats: dict[str, Any]) -> bool:
    win_rate = stats.get("win_rate")
    profit_factor = stats.get("profit_factor")
    max_drawdown = float(stats.get("max_drawdown") or 0.0)
    return (
        isinstance(win_rate, (int, float))
        and float(win_rate) >= SHADOW_FORWARD_MIN_WIN_RATE
        and isinstance(profit_factor, (int, float))
        and float(profit_factor) >= SHADOW_FORWARD_MIN_PROFIT_FACTOR
        and max_drawdown <= SHADOW_FORWARD_MAX_DRAWDOWN
    )


def _candidate_plan_openable(candidate: TradeCandidate, *, now: datetime, max_candidate_age_hours: float) -> str | None:
    return entry_plan_rules.candidate_plan_openable(
        direction=candidate.direction,
        entry_price=candidate.entry_price,
        stop_price=candidate.stop_price,
        target_price=candidate.target_price,
        setup_time=candidate.setup_time,
        now=now,
        max_candidate_age_hours=max_candidate_age_hours,
    )


def _within_entry_tolerance(price: float, planned_entry: float, *, entry_tolerance_pct: float) -> bool:
    return entry_plan_rules.within_entry_tolerance(
        price,
        planned_entry,
        entry_tolerance_pct=entry_tolerance_pct,
    )


def _candidate_entry_plan_state(
    candidate: TradeCandidate,
    *,
    mark_price: float,
    now: datetime,
    entry_tolerance_pct: float,
) -> dict[str, Any]:
    return entry_plan_rules.entry_plan_state(
        plan=_entry_plan(candidate),
        direction=candidate.direction,
        fallback_entry=candidate.entry_price,
        fallback_stop=candidate.stop_price,
        fallback_target=candidate.target_price,
        mark_price=mark_price,
        now=now,
        entry_tolerance_pct=entry_tolerance_pct,
    )


def _missing_price_entry_plan_state(candidate: TradeCandidate, *, now: datetime) -> dict[str, Any]:
    return entry_plan_rules.missing_price_entry_plan_state(
        plan=_entry_plan(candidate),
        direction=candidate.direction,
        fallback_entry=candidate.entry_price,
        fallback_stop=candidate.stop_price,
        fallback_target=candidate.target_price,
        now=now,
        entry_tolerance_pct=DEFAULT_ENTRY_TOLERANCE_PCT,
    )


def _entry_plan(candidate: TradeCandidate) -> dict[str, Any]:
    payload = candidate.decision_payload if isinstance(candidate.decision_payload, dict) else {}
    plan = payload.get("entry_plan") if isinstance(payload.get("entry_plan"), dict) else {}
    return dict(plan)


def _entry_range(
    plan: dict[str, Any],
    *,
    direction: str,
    planned_entry: float,
    planned_stop: float,
    target_price: float,
    entry_tolerance_pct: float,
) -> tuple[float, float, str]:
    return entry_plan_rules.entry_range(
        plan,
        direction=direction,
        planned_entry=planned_entry,
        planned_stop=planned_stop,
        target_price=target_price,
        entry_tolerance_pct=entry_tolerance_pct,
    )


def _entry_plan_shape_error(
    direction: str,
    *,
    entry: float,
    stop: float,
    target: float,
    lower: float,
    upper: float,
) -> str | None:
    return entry_plan_rules.entry_plan_shape_error(
        direction,
        entry=entry,
        stop=stop,
        target=target,
        lower=lower,
        upper=upper,
    )


def _price_invalidated(direction: str, *, mark_price: float, invalidation: float, stop: float) -> bool:
    return entry_plan_rules.price_invalidated(direction, mark_price=mark_price, invalidation=invalidation, stop=stop)


def _entry_range_missed(direction: str, *, mark_price: float, lower: float, upper: float) -> bool:
    return entry_plan_rules.entry_range_missed(direction, mark_price=mark_price, lower=lower, upper=upper)


def _target_price_from_plan(plan: dict[str, Any]) -> float | None:
    return entry_plan_rules.target_price_from_plan(plan)


def _shadow_trade_from_candidate(
    candidate: TradeCandidate,
    *,
    mark_price: float,
    now: datetime,
    entry_tolerance_pct: float,
    entry_plan_state: dict[str, Any] | None = None,
) -> ShadowPaperTrade | None:
    asset_tier = _asset_tier(candidate.symbol)
    entry_price = _execution_price(candidate.direction, mark_price, side="entry", asset_tier=asset_tier)
    if candidate.direction == "long" and not (candidate.stop_price < entry_price < candidate.target_price):
        return None
    if candidate.direction == "short" and not (candidate.target_price < entry_price < candidate.stop_price):
        return None
    signal_key = _shadow_forward_signal_key(candidate, opened_at=now)
    payload = candidate.decision_payload if isinstance(candidate.decision_payload, dict) else {}
    context = {
        "research_only": True,
        "shadow_forward": True,
        "opens_paper_trades": False,
        "opens_live_orders": False,
        "trade_candidate_id": candidate.id,
        "source_interaction_id": candidate.source_interaction_id,
        "lineage": candidate.lineage,
        "mark_price_at_signal": mark_price,
        "planned_entry_price": candidate.entry_price,
        "entry_tolerance_pct": entry_tolerance_pct,
        "entry_plan_state": entry_plan_state or {},
        "shadow_plan_fingerprint": _candidate_plan_fingerprint(candidate),
        "execution_model": _execution_model(asset_tier),
        "candidate_snapshot": _candidate_snapshot(candidate),
    }
    if isinstance(payload.get("alpha_sampling_gate"), dict):
        context["alpha_sampling_gate"] = dict(payload["alpha_sampling_gate"])
    return ShadowPaperTrade(
        strategy_name=DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        candidate_type="trade_candidate",
        candidate_key=candidate.candidate_key,
        signal_key=signal_key,
        source_experiment_run_id=None,
        symbol=candidate.symbol,
        timeframe=candidate.timeframe,
        direction=candidate.direction,
        entry_price=entry_price,
        stop_loss=candidate.stop_price,
        take_profit=candidate.target_price,
        position_size=1.0,
        status="open",
        mfe=0.0,
        mae=0.0,
        opened_at=now,
        context=context,
    )


def _apply_lifecycle(candidate: TradeCandidate) -> None:
    decision = decide_candidate_lifecycle(
        CandidateLifecycleEvidence(
            status=candidate.status,
            anti_repaint=AntiRepaintEvidence(candidate.anti_repaint_status),
            shadow=ShadowForwardEvidence(candidate.shadow_status),
            promotion_blockers=tuple(str(item) for item in (candidate.promotion_blockers or [])),
        )
    )
    candidate.promotion_blockers = decision.promotion_blockers
    candidate.paper_eligible = decision.paper_eligible
    candidate.live_eligible = decision.live_eligible
    candidate.promotion_status = decision.promotion_status


def _promotion_status(candidate: TradeCandidate) -> str:
    return candidate_promotion_status(
        status=candidate.status,
        anti_repaint_status=candidate.anti_repaint_status,
        shadow_status=candidate.shadow_status,
        promotion_blockers=candidate.promotion_blockers or [],
    )


def _ordered_blockers(blockers: set[str]) -> list[str]:
    return ordered_promotion_blockers(blockers)


def _shadow_forward_signal_key(candidate: TradeCandidate, *, opened_at: datetime) -> str:
    slot = opened_at.replace(minute=0, second=0, microsecond=0).isoformat()
    raw = f"{DARKFLOW_V2_SHADOW_STRATEGY_NAME}:{candidate.candidate_key}:{candidate.symbol}:{slot}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _candidate_snapshot(candidate: TradeCandidate) -> dict[str, Any]:
    return {
        "candidate_key": candidate.candidate_key,
        "strategy_id": candidate.strategy_id,
        "strategy_name": candidate.strategy_name,
        "symbol": candidate.symbol,
        "timeframe": candidate.timeframe,
        "interval": candidate.interval,
        "direction": candidate.direction,
        "setup_type": candidate.setup_type,
        "market_state": candidate.market_state,
        "entry_price": candidate.entry_price,
        "stop_price": candidate.stop_price,
        "target_price": candidate.target_price,
        "rr_ratio": candidate.rr_ratio,
        "quality_score": candidate.quality_score,
        "supporting_signals": candidate.supporting_signals,
    }


def _asset_tier(symbol: str) -> str:
    coin = symbol.removesuffix("USDT")
    if coin in {"BTC", "ETH"}:
        return "core"
    if coin in {"SOL", "BNB", "LINK", "TON"}:
        return "mainstream"
    return "high_volatility"


def _execution_price(direction: str, price: float, *, side: str, asset_tier: str) -> float:
    slippage = _slippage_rate(asset_tier)
    if side == "entry":
        worse = 1 + slippage if direction == "long" else 1 - slippage
    else:
        worse = 1 - slippage if direction == "long" else 1 + slippage
    return price * worse


def _slippage_rate(asset_tier: str) -> float:
    return SHADOW_SLIPPAGE_RATE_BY_TIER.get(asset_tier, SHADOW_SLIPPAGE_RATE_BY_TIER["high_volatility"])


def _execution_model(asset_tier: str) -> dict[str, Any]:
    return {
        "fee_rate_per_side": SHADOW_FEE_RATE,
        "round_trip_fee_rate": SHADOW_FEE_RATE * 2,
        "slippage_rate": _slippage_rate(asset_tier),
        "entry_and_exit_use_worse_price": True,
        "mode": "conservative_darkflow_v2_shadow_forward",
    }


def _shadow_criteria() -> dict[str, Any]:
    return {
        "min_closed_trades": SHADOW_FORWARD_MIN_CLOSED_TRADES,
        "min_win_rate": SHADOW_FORWARD_MIN_WIN_RATE,
        "min_profit_factor": SHADOW_FORWARD_MIN_PROFIT_FACTOR,
        "max_drawdown": SHADOW_FORWARD_MAX_DRAWDOWN,
    }


def _policy() -> dict[str, Any]:
    return {
        "research_only": True,
        "opens_paper_trades": False,
        "opens_live_orders": False,
        "paper_eligible_after_task": False,
        "live_eligible_after_task": False,
        "isolated_strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "isolated_table": "shadow_paper_trades",
        "promotion_boundary": "paper_review_ready is still manual review only; no real paper/live orders are opened here.",
    }


def _close(left: Any, right: Any, *, rel_tol: float = 1e-9, abs_tol: float = 1e-8) -> bool:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    left_f = float(left)
    right_f = float(right)
    return abs(left_f - right_f) <= max(abs_tol, rel_tol * max(abs(left_f), abs(right_f), 1.0))


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


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value is not None else None


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        if peak:
            drawdown = max(drawdown, (peak - equity) / peak)
    return drawdown


def _age_minutes(now: datetime, value: datetime | None) -> float | None:
    if value is None:
        return None
    return round(max(0.0, (_aware(now) - _aware(value)).total_seconds() / 60.0), 3)


def _freshest_age_minutes(now: datetime, *values: datetime | None) -> float | None:
    ages = [_age_minutes(now, value) for value in values]
    valid_ages = [age for age in ages if age is not None]
    return min(valid_ages) if valid_ages else None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
