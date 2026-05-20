from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.shadow_forward_samples import (
    candidate_snapshot as _candidate_snapshot,
    shadow_plan_horizon as _trade_horizon,
    unique_shadow_plans,
)
from app.domain.setup_expectancy import setup_expectancy_rows
from app.domain.trade_outcomes import build_trade_outcome, max_drawdown, summarize_trade_outcomes
from app.domain.whitelist_blacklist_policy import classify_setup_expectancy, strategy_action_from_expectancy
from app.models import FeatureEvent, FeatureLabel, PriceSnapshot, ShadowPaperTrade
from app.services.feature_candidates import latest_feature_segment_candidate_screen
from app.services.features import FEATURE_HORIZONS
from app.services.paper import (
    _extend_take_profit_runner,
    _pnl,
    _runner_asset_eligible,
    _runner_decision,
    _runner_extension_allowed,
    _stop_exit_reason,
    _take_profit_touched,
)
from app.services.research_lineage import legacy_feature_research_lineage
from app.services.risk import template_for_tier


SHADOW_STRATEGY_NAME = "shadow_feature_candidates_v1"
DARKFLOW_V2_SHADOW_STRATEGY_NAME = "darkflow_v2_trade_candidate_shadow_forward_v1"
DARKFLOW_SHADOW_FORWARD_TIME_EXIT_REASON = "shadow_forward_time_exit"
SHADOW_FEE_RATE = 0.0004
SHADOW_SLIPPAGE_RATE_BY_TIER = {
    "core": 0.0002,
    "mainstream": 0.00035,
    "high_volatility": 0.0007,
}
PROMOTION_MIN_CLOSED_TRADES = 30
PROMOTION_MIN_WIN_RATE = 0.52
PROMOTION_MIN_PROFIT_FACTOR = 1.25
PROMOTION_MAX_DRAWDOWN = 0.12
DEFAULT_SHADOW_REPLAY_LIMIT = 500
DARKFLOW_RECOMMENDATION_WHITELIST_MIN_CLOSED = 5
DARKFLOW_RECOMMENDATION_PAUSE_MIN_CLOSED = 3
DARKFLOW_STRATEGY_ACTION_MIN_CLOSED = 10
DARKFLOW_TIME_EXIT_REASON = DARKFLOW_SHADOW_FORWARD_TIME_EXIT_REASON
DARKFLOW_SAMPLE_REVIEW_TARGET = 30
DARKFLOW_SAMPLE_VALIDATION_TARGET = 100
DARKFLOW_SAMPLE_PRE_PAPER_TARGET = 200
DARKFLOW_SAMPLE_TARGETS = {
    "first_review": DARKFLOW_SAMPLE_REVIEW_TARGET,
    "validation": DARKFLOW_SAMPLE_VALIDATION_TARGET,
    "pre_paper": DARKFLOW_SAMPLE_PRE_PAPER_TARGET,
}
DARKFLOW_TIME_EXIT_REVIEW_WINDOWS_MINUTES = (30, 60, 120, 240)
DARKFLOW_TIME_EXIT_EXTENSION_MIN_SAMPLES = 3
DARKFLOW_TIME_EXIT_EXTENSION_MIN_DELTA = 0.002
DARKFLOW_TIME_EXIT_EXTENSION_MIN_IMPROVED_RATE = 0.60


async def shadow_paper_scan(
    session: AsyncSession,
    *,
    candidate_limit: int = 50,
    include_watchlist: bool = True,
) -> dict[str, Any]:
    report = await latest_feature_segment_candidate_screen(session, horizon="30m")
    source_experiment_run_id = report.get("source_experiment_run_id")
    candidates = _shadow_candidate_rows(report, candidate_limit=candidate_limit, include_watchlist=include_watchlist)
    opened: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row, candidate_type in candidates:
        symbol = str(row.get("symbol") or "")
        direction = str(row.get("direction") or "")
        timeframe = str(row.get("timeframe") or "")
        candidate_key = str(row.get("segment_key") or row.get("feature_key") or "")
        if not symbol or direction not in {"long", "short"} or not candidate_key:
            skipped.append({"candidate_key": candidate_key, "reason": "incomplete_candidate"})
            continue
        existing_open = await _open_shadow_trade(session, candidate_key=candidate_key, symbol=symbol)
        if existing_open is not None:
            skipped.append({"candidate_key": candidate_key, "symbol": symbol, "reason": "open_shadow_trade_exists"})
            continue
        price = await _latest_price(session, symbol)
        if price is None or price <= 0:
            skipped.append({"candidate_key": candidate_key, "symbol": symbol, "reason": "missing_price"})
            continue
        asset_tier = _asset_tier(symbol)
        entry_price = _execution_price(direction, price, side="entry", asset_tier=asset_tier)
        levels = _shadow_levels(direction, entry_price, asset_tier)
        signal_key = _signal_key(candidate_key=candidate_key, symbol=symbol, direction=direction, price=price)
        trade = ShadowPaperTrade(
            strategy_name=SHADOW_STRATEGY_NAME,
            candidate_type=candidate_type,
            candidate_key=candidate_key,
            signal_key=signal_key,
            source_experiment_run_id=str(source_experiment_run_id) if source_experiment_run_id else None,
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            entry_price=entry_price,
            stop_loss=levels["stop_loss"],
            take_profit=levels["take_profit"],
            position_size=1.0,
            status="open",
            context={
                "research_only": True,
                "opens_paper_trades": False,
                "mark_price_at_signal": price,
                "execution_model": _execution_model(asset_tier),
                "candidate_snapshot": _candidate_context(row),
            },
        )
        session.add(trade)
        await session.flush()
        opened.append({"id": trade.id, "symbol": symbol, "candidate_key": candidate_key, "direction": direction})
    if opened:
        await session.commit()
    return {
        "strategy_name": SHADOW_STRATEGY_NAME,
        "source_experiment_run_id": source_experiment_run_id,
        "opened": opened,
        "skipped": skipped,
        "policy": _shadow_policy(),
    }


async def shadow_paper_replay(
    session: AsyncSession,
    *,
    horizon: str = "30m",
    limit: int = DEFAULT_SHADOW_REPLAY_LIMIT,
    candidate_limit: int = 50,
    include_watchlist: bool = True,
) -> dict[str, Any]:
    if horizon not in FEATURE_HORIZONS:
        raise ValueError(f"unsupported horizon: {horizon}")
    report = await latest_feature_segment_candidate_screen(session, horizon=horizon)
    source_experiment_run_id = report.get("source_experiment_run_id")
    candidate_rows = _shadow_candidate_rows(
        report,
        candidate_limit=candidate_limit,
        include_watchlist=include_watchlist,
    )
    if not candidate_rows:
        return {
            "strategy_name": SHADOW_STRATEGY_NAME,
            "horizon": horizon,
            "requested_limit": limit,
            "candidate_limit": candidate_limit,
            "source_experiment_run_id": source_experiment_run_id,
            "inserted": 0,
            "duplicates": 0,
            "skipped": [{"reason": "no_shadow_candidates"}],
            "policy": _shadow_policy(),
        }
    pairs = await _replay_feature_pairs(
        session,
        candidate_rows=candidate_rows,
        horizon=horizon,
        limit=max(1, limit) * 3,
    )
    planned = []
    for event, label, row, candidate_type in pairs:
        planned.append(
            _replay_plan(
                event,
                label,
                row=row,
                candidate_type=candidate_type,
                source_experiment_run_id=str(source_experiment_run_id) if source_experiment_run_id else None,
                horizon=horizon,
            )
        )
    planned = [item for item in planned if item is not None]
    signal_keys = [item["signal_key"] for item in planned]
    existing_keys = await _existing_shadow_signal_keys(session, signal_keys)
    inserted = 0
    duplicates = 0
    skipped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in planned:
        if inserted >= max(0, limit):
            break
        signal_key = item["signal_key"]
        if signal_key in existing_keys or signal_key in seen_keys:
            duplicates += 1
            continue
        seen_keys.add(signal_key)
        session.add(item["trade"])
        inserted += 1
    if inserted:
        await session.commit()
    elif planned:
        await session.rollback()
    return {
        "strategy_name": SHADOW_STRATEGY_NAME,
        "horizon": horizon,
        "requested_limit": limit,
        "candidate_limit": candidate_limit,
        "source_experiment_run_id": source_experiment_run_id,
        "candidate_rows": len(candidate_rows),
        "pairs_scanned": len(pairs),
        "planned": len(planned),
        "inserted": inserted,
        "duplicates": duplicates,
        "skipped": skipped,
        "policy": _shadow_policy(),
    }


async def shadow_paper_replay_all(
    session: AsyncSession,
    *,
    horizons: list[str] | None = None,
    limit: int = DEFAULT_SHADOW_REPLAY_LIMIT,
    candidate_limit: int = 50,
    include_watchlist: bool = True,
) -> dict[str, Any]:
    selected_horizons = horizons or list(FEATURE_HORIZONS)
    unsupported = [item for item in selected_horizons if item not in FEATURE_HORIZONS]
    if unsupported:
        raise ValueError(f"unsupported horizons: {', '.join(unsupported)}")
    results: dict[str, Any] = {}
    total_inserted = 0
    total_duplicates = 0
    for horizon in selected_horizons:
        result = await shadow_paper_replay(
            session,
            horizon=horizon,
            limit=limit,
            candidate_limit=candidate_limit,
            include_watchlist=include_watchlist,
        )
        results[horizon] = result
        total_inserted += int(result.get("inserted") or 0)
        total_duplicates += int(result.get("duplicates") or 0)
    return {
        "strategy_name": SHADOW_STRATEGY_NAME,
        "horizons": selected_horizons,
        "limit_per_horizon": limit,
        "candidate_limit": candidate_limit,
        "inserted": total_inserted,
        "duplicates": total_duplicates,
        "results": results,
        "policy": _shadow_policy(),
    }


async def mark_shadow_paper_trades(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(
        select(ShadowPaperTrade).where(ShadowPaperTrade.status == "open").order_by(ShadowPaperTrade.opened_at)
    )
    closed: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    for trade in rows.scalars().all():
        now = datetime.now(timezone.utc)
        price = await _latest_price(session, trade.symbol)
        if price is None:
            time_exit_context = _darkflow_shadow_forward_time_exit_context(trade, now=now)
            fallback_price = _darkflow_shadow_forward_fallback_mark_price(trade)
            if time_exit_context is None or fallback_price is None:
                continue
            price, fallback_source = fallback_price
            exit_reason = DARKFLOW_SHADOW_FORWARD_TIME_EXIT_REASON
            exit_context: dict[str, Any] = {
                **time_exit_context,
                "missing_latest_price_at_time_exit": True,
                "fallback_mark_price_source": fallback_source,
            }
            runner_decision: dict[str, Any] | None = None
        else:
            exit_reason = _stop_exit_reason(trade, price)
            exit_context = {}
            runner_decision = None
        mark_pnl = _pnl(trade.direction, trade.entry_price, price)
        pnl = _net_pnl(trade, price, exit_side="mark")
        trade.mfe = max(trade.mfe, pnl)
        trade.mae = min(trade.mae, pnl)
        previous_stop_loss = trade.stop_loss
        previous_take_profit = trade.take_profit
        if exit_reason is None and _take_profit_touched(trade, price):
            runner_decision = _shadow_runner_decision(trade, price)
            if runner_decision["extend"]:
                _extend_take_profit_runner(trade, price)
            else:
                exit_reason = "take_profit"
        if exit_reason is None:
            time_exit_context = _darkflow_shadow_forward_time_exit_context(trade, now=now)
            if time_exit_context is not None:
                exit_reason = DARKFLOW_SHADOW_FORWARD_TIME_EXIT_REASON
                exit_context = time_exit_context
        if exit_reason:
            exit_price = _execution_price(trade.direction, price, side="exit", asset_tier=_asset_tier(trade.symbol))
            pnl = _net_pnl(trade, exit_price, exit_side="executed")
            trade.status = "closed"
            trade.exit_price = exit_price
            trade.exit_reason = exit_reason
            trade.pnl = pnl
            stop_pct = abs(trade.entry_price - trade.stop_loss) / trade.entry_price
            trade.r_multiple = pnl / stop_pct if stop_pct else 0.0
            trade.closed_at = now
            trade.context = _merge_context(
                trade.context,
                {
                    "last_mark_price": price,
                    "exit_mark_price": price,
                    "exit_execution_price": exit_price,
                    "gross_pnl_before_cost": _pnl(trade.direction, trade.entry_price, exit_price),
                    "net_pnl_after_cost": pnl,
                    "total_fee_rate": SHADOW_FEE_RATE * 2,
                    "closed_by_shadow_mark": True,
                    "runner_decision": runner_decision,
                    **exit_context,
                },
            )
            closed.append({"id": trade.id, "symbol": trade.symbol, "exit_reason": exit_reason, "pnl": pnl, "mark_pnl": mark_pnl})
        else:
            context_update: dict[str, Any] = {"last_mark_price": price, "net_mark_pnl_after_cost": pnl}
            payload = {"id": trade.id, "symbol": trade.symbol, "pnl": pnl, "mark_pnl": mark_pnl}
            if trade.stop_loss != previous_stop_loss or trade.take_profit != previous_take_profit:
                context_update.update(
                    {
                        "runner_decision": runner_decision,
                        "runner_extended_at": now.isoformat(),
                        "previous_stop_loss": previous_stop_loss,
                        "previous_take_profit": previous_take_profit,
                    }
                )
                payload.update(
                    {
                        "runner_extended": True,
                        "runner_evidence": runner_decision,
                        "stop_loss": trade.stop_loss,
                        "take_profit": trade.take_profit,
                        "previous_stop_loss": previous_stop_loss,
                        "previous_take_profit": previous_take_profit,
                    }
                )
            trade.context = _merge_context(trade.context, context_update)
            updated.append(payload)
    if closed or updated:
        await session.commit()
    return {"closed": closed, "updated": updated, "policy": _shadow_policy()}


async def shadow_paper_trades(
    session: AsyncSession,
    *,
    limit: int = 50,
    strategy_name: str | None = None,
) -> list[dict[str, Any]]:
    query = select(ShadowPaperTrade)
    if strategy_name:
        query = query.where(ShadowPaperTrade.strategy_name == strategy_name)
    rows = await session.execute(query.order_by(ShadowPaperTrade.opened_at.desc()).limit(limit))
    return [_trade_payload(item) for item in rows.scalars().all()]


async def shadow_paper_stats(session: AsyncSession, *, strategy_name: str | None = None) -> dict[str, Any]:
    query = select(ShadowPaperTrade)
    if strategy_name:
        query = query.where(ShadowPaperTrade.strategy_name == strategy_name)
    rows = await session.execute(query)
    trades = rows.scalars().all()
    totals = _trade_stats(trades)
    unique_plan_trades = _unique_plan_trades(trades)
    unique_plan_stats = _trade_stats(unique_plan_trades)
    by_candidate = _grouped_trade_stats(trades, key_func=_candidate_group_key)[:20]
    return {
        "strategy_name": strategy_name or "all_shadow_strategies",
        **totals,
        "unique_plan_stats": {
            **unique_plan_stats,
            "source_trade_count": len(trades),
            "duplicate_trade_count": max(0, len(trades) - len(unique_plan_trades)),
            "dedupe_method": "shadow_plan_fingerprint_or_price_bucket",
        },
        "by_candidate": by_candidate,
        "by_horizon": _grouped_trade_stats(trades, key_func=_horizon_group_key)[:20],
        "by_symbol": _grouped_trade_stats(trades, key_func=_symbol_group_key)[:20],
        "promotion": _promotion_report(by_candidate),
        "policy": _shadow_policy(),
    }


async def shadow_paper_promotion_report(session: AsyncSession) -> dict[str, Any]:
    stats = await shadow_paper_stats(session)
    return {
        "strategy_name": stats["strategy_name"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "promotion": stats["promotion"],
        "policy": stats["policy"],
    }


async def darkflow_playbook_attribution_report(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(
        select(ShadowPaperTrade)
        .where(ShadowPaperTrade.strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME)
        .order_by(ShadowPaperTrade.opened_at.desc())
    )
    trades = _unique_plan_trades(list(rows.scalars().all()))
    buckets: dict[tuple[str, str, str, str], list[ShadowPaperTrade]] = {}
    for trade in trades:
        snapshot = _candidate_snapshot(trade)
        strategy_id = str(snapshot.get("strategy_id") or "unknown")
        market_state = str(snapshot.get("market_state") or "unknown")
        buckets.setdefault((strategy_id, trade.symbol, trade.direction, market_state), []).append(trade)
    report_rows: list[dict[str, Any]] = []
    for (strategy_id, symbol, direction, market_state), items in buckets.items():
        stats = _trade_stats(items)
        exit_reason_counts = _exit_reason_counts(items)
        report_rows.append(
            {
                "strategy_id": strategy_id,
                "strategy_name": str(_candidate_snapshot(items[0]).get("strategy_name") or strategy_id),
                "symbol": symbol,
                "direction": direction,
                "market_state": market_state,
                **stats,
                "exit_reason_counts": exit_reason_counts,
            }
        )
    report_rows.sort(key=lambda item: (int(item.get("closed_trades") or 0), float(item.get("profit_factor") or -999.0)), reverse=True)
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": report_rows,
        "policy": _shadow_policy() | {"report_only": True, "lineage": "core_darkflow_v2"},
    }


async def darkflow_trend_extension_exit_report(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(
        select(ShadowPaperTrade)
        .where(ShadowPaperTrade.strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME)
        .order_by(ShadowPaperTrade.opened_at.desc())
    )
    trades = [
        trade
        for trade in _unique_plan_trades(list(rows.scalars().all()))
        if str(_candidate_snapshot(trade).get("strategy_id") or "") == "trend_ride_extension"
    ]
    hold_minutes = sorted(_hold_minutes(trade) for trade in trades if _hold_minutes(trade) is not None)
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "strategy_id": "trend_ride_extension",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **_trade_stats(trades),
        "exit_reason_counts": _exit_reason_counts(trades),
        "median_hold_minutes": _median_minutes(hold_minutes),
        "rows": [_trade_payload(trade) for trade in trades[:50]],
        "policy": _shadow_policy() | {"report_only": True, "lineage": "core_darkflow_v2"},
    }


async def darkflow_time_exit_review_report(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(
        select(ShadowPaperTrade)
        .where(
            ShadowPaperTrade.strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME,
            ShadowPaperTrade.status == "closed",
            ShadowPaperTrade.exit_reason == DARKFLOW_TIME_EXIT_REASON,
            ShadowPaperTrade.closed_at.is_not(None),
            ShadowPaperTrade.exit_price.is_not(None),
        )
        .order_by(ShadowPaperTrade.closed_at.desc())
    )
    trades = _unique_plan_trades(list(rows.scalars().all()))
    post_exit_by_trade = await _time_exit_post_exit_windows(session, trades)
    global_summary = _time_exit_global_summary(trades, post_exit_by_trade)
    group_rows = _time_exit_group_rows(trades, post_exit_by_trade)
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "time_exit_trade_count": len(trades),
        "windows_minutes": list(DARKFLOW_TIME_EXIT_REVIEW_WINDOWS_MINUTES),
        **global_summary,
        "rows": group_rows,
        "thresholds": {
            "extension_min_samples": DARKFLOW_TIME_EXIT_EXTENSION_MIN_SAMPLES,
            "extension_min_delta": DARKFLOW_TIME_EXIT_EXTENSION_MIN_DELTA,
            "extension_min_improved_rate": DARKFLOW_TIME_EXIT_EXTENSION_MIN_IMPROVED_RATE,
        },
        "policy": _shadow_policy()
        | {
            "report_only": True,
            "lineage": "core_darkflow_v2",
            "mutates_trades": False,
            "mutates_exit_rules": False,
            "purpose": "review whether time exits should be extended per darkflow sub-portfolio",
        },
    }


async def darkflow_subportfolio_recommendations_report(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(
        select(ShadowPaperTrade)
        .where(ShadowPaperTrade.strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME)
        .order_by(ShadowPaperTrade.opened_at.desc())
    )
    trades = _unique_plan_trades(list(rows.scalars().all()))
    group_rows = _darkflow_subportfolio_rows(trades)
    strategy_rows = _darkflow_strategy_action_rows(trades)
    strategy_action_by_id = {str(item["strategy_id"]): item for item in strategy_rows}
    for row in group_rows:
        strategy_action = strategy_action_by_id.get(str(row["strategy_id"])) or {}
        row["main_path_action"] = strategy_action.get("main_path_action", "collect_more")
        row["main_path_action_text"] = strategy_action.get("action_text", "继续补样，暂不提高主路径权重。")
        row["main_path_weight_multiplier"] = strategy_action.get("weight_multiplier", 1.0)
    counts: dict[str, int] = {}
    for row in group_rows:
        key = str(row["recommendation"])
        counts[key] = counts.get(key, 0) + 1
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dimension": "strategy_id+symbol+direction+market_state",
        "rows": group_rows,
        "recommendation_counts": counts,
        "strategy_actions": strategy_rows,
        "thresholds": {
            "whitelist_min_closed_trades": DARKFLOW_RECOMMENDATION_WHITELIST_MIN_CLOSED,
            "pause_min_closed_trades": DARKFLOW_RECOMMENDATION_PAUSE_MIN_CLOSED,
            "strategy_action_min_closed_trades": DARKFLOW_STRATEGY_ACTION_MIN_CLOSED,
            "sample_first_review_target": DARKFLOW_SAMPLE_REVIEW_TARGET,
            "sample_validation_target": DARKFLOW_SAMPLE_VALIDATION_TARGET,
            "sample_pre_paper_target": DARKFLOW_SAMPLE_PRE_PAPER_TARGET,
            "whitelist_min_win_rate": 0.60,
            "whitelist_min_profit_factor": 1.50,
            "pause_max_win_rate": 0.35,
            "pause_max_profit_factor": 0.85,
            "deweight_max_profit_factor": 1.0,
            "time_exit_share_limit": 0.65,
        },
        "policy": _shadow_policy()
        | {
            "report_only": True,
            "lineage": "core_darkflow_v2",
            "mutates_candidates": False,
            "mutates_weights": False,
            "purpose": "rank darkflow shadow-forward sub-portfolios before any paper/live promotion",
        },
    }


async def darkflow_setup_expectancy_report(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(
        select(ShadowPaperTrade)
        .where(ShadowPaperTrade.strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME)
        .order_by(ShadowPaperTrade.opened_at.desc())
    )
    trades = _unique_plan_trades(list(rows.scalars().all()))
    expectancy_rows = setup_expectancy_rows(trades, evidence_source="shadow_forward")
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dimension": "strategy_family+setup_type+strategy_id+symbol+direction+timeframe+market_state+evidence_source",
        "rows": expectancy_rows,
        "evidence_sources": {
            "shadow_forward": "Core Darkflow v2 影子前向样本",
            "paper": "真实纸上交易",
            "backtest": "回测证据",
            "legacy_control": "Legacy/Control Research 对照，不可用于开仓晋级",
        },
        "policy": _shadow_policy()
        | {
            "report_only": True,
            "lineage": "core_darkflow_v2",
            "mutates_candidates": False,
            "mutates_weights": False,
            "opens_paper_trades": False,
            "opens_live_orders": False,
            "legacy_control_can_promote": False,
            "purpose": "aggregate Core Darkflow v2 setup expectancy evidence before whitelist or paper review",
        },
    }


def _darkflow_subportfolio_rows(trades: list[ShadowPaperTrade]) -> list[dict[str, Any]]:
    expectancy_rows = setup_expectancy_rows(trades, evidence_source="shadow_forward")
    rows: list[dict[str, Any]] = []
    for expectancy in expectancy_rows:
        stats = dict(expectancy)
        exit_counts = dict(expectancy.get("exit_reason_counts") or {})
        recommendation, reasons = _darkflow_subportfolio_recommendation(stats, exit_counts)
        sample_progress = _darkflow_sample_progress(int(stats.get("closed_trades") or 0), recommendation=recommendation)
        subportfolio_key = "|".join(
            [
                str(expectancy["strategy_id"]),
                str(expectancy["symbol"]),
                str(expectancy["direction"]),
                str(expectancy["market_state"]),
            ]
        )
        rows.append(
            {
                **stats,
                "group_key": subportfolio_key,
                "setup_expectancy_key": expectancy["group_key"],
                "strategy_id": expectancy["strategy_id"],
                "strategy_name": expectancy["strategy_name"],
                "symbol": expectancy["symbol"],
                "direction": expectancy["direction"],
                "timeframe": expectancy["timeframe"],
                "strategy_family": expectancy["strategy_family"],
                "setup_type": expectancy["setup_type"],
                "market_state": expectancy["market_state"],
                "evidence_source": expectancy["evidence_source"],
                "exit_reason_counts": exit_counts,
                "recommendation": recommendation,
                "recommendation_text": _darkflow_recommendation_text(recommendation),
                "sampling_action": _darkflow_sampling_action(recommendation),
                "confidence": _recommendation_confidence(int(stats.get("closed_trades") or 0)),
                "reasons": reasons,
                **sample_progress,
            }
        )
    return sorted(
        rows,
        key=lambda item: (
            _darkflow_recommendation_rank(str(item["recommendation"])),
            int(item.get("closed_trades") or 0),
            float(item.get("profit_factor") or -999.0),
        ),
        reverse=True,
    )


def _darkflow_strategy_action_rows(trades: list[ShadowPaperTrade]) -> list[dict[str, Any]]:
    buckets: dict[str, list[ShadowPaperTrade]] = {}
    for trade in trades:
        strategy_id = str(_candidate_snapshot(trade).get("strategy_id") or "unknown")
        buckets.setdefault(strategy_id, []).append(trade)
    rows: list[dict[str, Any]] = []
    for strategy_id, items in buckets.items():
        stats = _trade_stats(items)
        exit_counts = _exit_reason_counts(items)
        action = strategy_action_from_expectancy(
            {
                **stats,
                "exit_reason_counts": exit_counts,
                "time_exit_share": _exit_reason_share(exit_counts, DARKFLOW_TIME_EXIT_REASON),
                "evidence_source": "shadow_forward",
            }
        )
        rows.append(
            {
                "strategy_id": strategy_id,
                "strategy_name": str(_candidate_snapshot(items[0]).get("strategy_name") or strategy_id),
                **stats,
                "exit_reason_counts": exit_counts,
                "time_exit_share": _exit_reason_share(exit_counts, DARKFLOW_TIME_EXIT_REASON),
                "main_path_action": action["main_path_action"],
                "action_text": action["action_text"],
                "weight_multiplier": action["weight_multiplier"],
                "reason_codes": action["reason_codes"],
                "reasons": action["reasons"],
            }
        )
    return sorted(
        rows,
        key=lambda item: (
            _darkflow_strategy_action_rank(str(item["main_path_action"])),
            int(item.get("closed_trades") or 0),
            float(item.get("profit_factor") or -999.0),
        ),
        reverse=True,
    )


def _darkflow_subportfolio_recommendation(stats: dict[str, Any], exit_counts: dict[str, int]) -> tuple[str, list[str]]:
    decision = classify_setup_expectancy({**stats, "exit_reason_counts": exit_counts})
    recommendation = "keep_sampling" if decision["classification"] == "collecting" else str(decision["classification"])
    return recommendation, list(decision["reasons"])


def _darkflow_sample_progress(closed: int, *, recommendation: str) -> dict[str, Any]:
    if closed < DARKFLOW_SAMPLE_REVIEW_TARGET:
        next_target = DARKFLOW_SAMPLE_REVIEW_TARGET
        stage = "first_review"
    elif closed < DARKFLOW_SAMPLE_VALIDATION_TARGET:
        next_target = DARKFLOW_SAMPLE_VALIDATION_TARGET
        stage = "validation"
    elif closed < DARKFLOW_SAMPLE_PRE_PAPER_TARGET:
        next_target = DARKFLOW_SAMPLE_PRE_PAPER_TARGET
        stage = "pre_paper"
    else:
        next_target = None
        stage = "mature"
    denominator = next_target or DARKFLOW_SAMPLE_PRE_PAPER_TARGET
    return {
        "sample_targets": dict(DARKFLOW_SAMPLE_TARGETS),
        "next_sample_target": next_target,
        "remaining_to_next_target": max(0, int(next_target or DARKFLOW_SAMPLE_PRE_PAPER_TARGET) - closed),
        "sample_progress": min(1.0, closed / denominator) if denominator else 1.0,
        "sample_stage": stage,
        "paper_review_ready": recommendation == "whitelist" and closed >= DARKFLOW_SAMPLE_REVIEW_TARGET,
    }


def _darkflow_strategy_action(stats: dict[str, Any], exit_counts: dict[str, int]) -> tuple[str, list[str]]:
    closed = int(stats.get("closed_trades") or 0)
    win_rate = _number(stats.get("win_rate"))
    profit_factor = _number(stats.get("profit_factor"))
    max_drawdown = _number(stats.get("max_drawdown"))
    time_exit_share = _exit_reason_share(exit_counts, DARKFLOW_TIME_EXIT_REASON)
    if closed < DARKFLOW_STRATEGY_ACTION_MIN_CLOSED:
        return "collect_more", [f"策略级样本只有 {closed} 笔，先继续隔离补样。"]
    if _at_least(win_rate, 0.55) and _at_least(profit_factor, 1.25) and _at_most(max_drawdown, 0.12):
        return "keep", ["策略级前向胜率、盈利因子和回撤达到主路径保留线。"]
    if (profit_factor is not None and profit_factor < 1.0) or (win_rate is not None and win_rate < 0.45) or (time_exit_share is not None and time_exit_share >= 0.65):
        reasons = ["策略级前向结果不足以继续占用主路径权重。"]
        if time_exit_share is not None and time_exit_share >= 0.65:
            reasons.append("时间退出占比过高，说明该玩法在当前实现中缺少有效退出优势。")
        return "deweight", reasons
    return "review", ["策略级表现不够强，也没有弱到需要整体移出，保持人工复核。"]


async def _time_exit_post_exit_windows(session: AsyncSession, trades: list[ShadowPaperTrade]) -> dict[str, dict[int, dict[str, Any]]]:
    payload: dict[str, dict[int, dict[str, Any]]] = {}
    for trade in trades:
        if trade.closed_at is None or trade.exit_price is None:
            continue
        closed_at = _aware(trade.closed_at)
        max_minutes = max(DARKFLOW_TIME_EXIT_REVIEW_WINDOWS_MINUTES)
        price_rows = await session.execute(
            select(PriceSnapshot)
            .where(
                PriceSnapshot.symbol == trade.symbol,
                PriceSnapshot.collected_at > closed_at,
                PriceSnapshot.collected_at <= closed_at + timedelta(minutes=max_minutes),
            )
            .order_by(PriceSnapshot.collected_at.asc())
        )
        prices = list(price_rows.scalars().all())
        if not prices:
            continue
        trade_payload: dict[int, dict[str, Any]] = {}
        for minutes in DARKFLOW_TIME_EXIT_REVIEW_WINDOWS_MINUTES:
            window_prices = [item for item in prices if _aware(item.collected_at) <= closed_at + timedelta(minutes=minutes)]
            if not window_prices:
                continue
            last_price = float(window_prices[-1].price)
            total_pnl = _time_exit_net_pnl_if_held(trade, last_price)
            incremental = _pnl(trade.direction, float(trade.exit_price), last_price) if trade.exit_price else None
            hit, hit_at = _time_exit_first_hit(trade, window_prices)
            trade_payload[minutes] = {
                "last_price": last_price,
                "last_price_at": window_prices[-1].collected_at,
                "total_pnl_if_held": total_pnl,
                "incremental_after_exit": incremental,
                "delta_vs_actual": total_pnl - float(trade.pnl or 0.0),
                "mfe_after_exit": _time_exit_mfe_after_exit(trade, window_prices),
                "mae_after_exit": _time_exit_mae_after_exit(trade, window_prices),
                "first_hit_after_exit": hit,
                "first_hit_at": hit_at,
            }
        if trade_payload:
            payload[trade.id] = trade_payload
    return payload


def _time_exit_net_pnl_if_held(trade: ShadowPaperTrade, mark_price: float) -> float:
    exit_price = _execution_price(trade.direction, mark_price, side="exit", asset_tier=_asset_tier(trade.symbol))
    return _net_pnl(trade, exit_price, exit_side="executed")


def _time_exit_first_hit(trade: ShadowPaperTrade, prices: list[PriceSnapshot]) -> tuple[str | None, str | None]:
    for item in prices:
        price = float(item.price)
        if trade.direction == "long":
            hit = "stop_loss" if price <= trade.stop_loss else "take_profit" if price >= trade.take_profit else None
        else:
            hit = "stop_loss" if price >= trade.stop_loss else "take_profit" if price <= trade.take_profit else None
        if hit:
            return hit, item.collected_at.isoformat()
    return None, None


def _time_exit_mfe_after_exit(trade: ShadowPaperTrade, prices: list[PriceSnapshot]) -> float | None:
    if trade.exit_price is None or not prices:
        return None
    return max(_pnl(trade.direction, float(trade.exit_price), float(item.price)) for item in prices)


def _time_exit_mae_after_exit(trade: ShadowPaperTrade, prices: list[PriceSnapshot]) -> float | None:
    if trade.exit_price is None or not prices:
        return None
    return min(_pnl(trade.direction, float(trade.exit_price), float(item.price)) for item in prices)


def _time_exit_global_summary(trades: list[ShadowPaperTrade], windows_by_trade: dict[str, dict[int, dict[str, Any]]]) -> dict[str, Any]:
    actual = [float(trade.pnl) for trade in trades if isinstance(trade.pnl, (int, float))]
    return {
        "actual": _time_exit_actual_stats(actual),
        "windows": {
            str(minutes): _time_exit_window_stats(trades, windows_by_trade, minutes)
            for minutes in DARKFLOW_TIME_EXIT_REVIEW_WINDOWS_MINUTES
        },
    }


def _time_exit_group_rows(trades: list[ShadowPaperTrade], windows_by_trade: dict[str, dict[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str, str], list[ShadowPaperTrade]] = {}
    for trade in trades:
        snapshot = _candidate_snapshot(trade)
        strategy_id = str(snapshot.get("strategy_id") or "unknown")
        market_state = str(snapshot.get("market_state") or "unknown")
        buckets.setdefault((strategy_id, trade.symbol, trade.direction, market_state), []).append(trade)
    rows: list[dict[str, Any]] = []
    for (strategy_id, symbol, direction, market_state), items in buckets.items():
        windows = {
            str(minutes): _time_exit_window_stats(items, windows_by_trade, minutes)
            for minutes in DARKFLOW_TIME_EXIT_REVIEW_WINDOWS_MINUTES
        }
        best_window, action, reasons = _time_exit_action(windows)
        rows.append(
            {
                "group_key": "|".join([strategy_id, symbol, direction, market_state]),
                "strategy_id": strategy_id,
                "strategy_name": str(_candidate_snapshot(items[0]).get("strategy_name") or strategy_id),
                "symbol": symbol,
                "direction": direction,
                "market_state": market_state,
                "time_exit_trades": len(items),
                "actual": _time_exit_actual_stats([float(item.pnl) for item in items if isinstance(item.pnl, (int, float))]),
                "windows": windows,
                "best_window_minutes": best_window,
                "action": action,
                "action_text": _time_exit_action_text(action),
                "reasons": reasons,
            }
        )
    return sorted(rows, key=_time_exit_row_rank, reverse=True)


def _time_exit_window_stats(trades: list[ShadowPaperTrade], windows_by_trade: dict[str, dict[int, dict[str, Any]]], minutes: int) -> dict[str, Any]:
    windows = [windows_by_trade.get(trade.id, {}).get(minutes) for trade in trades]
    windows = [item for item in windows if item is not None]
    total = [_number(item.get("total_pnl_if_held")) for item in windows]
    incremental = [_number(item.get("incremental_after_exit")) for item in windows]
    deltas = [_number(item.get("delta_vs_actual")) for item in windows]
    hits = [item.get("first_hit_after_exit") for item in windows]
    return {
        "coverage": len(windows),
        "coverage_rate": len(windows) / len(trades) if trades else None,
        "avg_total_pnl_if_held": _mean_number(total),
        "median_total_pnl_if_held": _median_number(total),
        "win_rate_if_held": _positive_rate(total),
        "avg_incremental_after_exit": _mean_number(incremental),
        "median_incremental_after_exit": _median_number(incremental),
        "avg_delta_vs_actual": _mean_number(deltas),
        "median_delta_vs_actual": _median_number(deltas),
        "improved_rate": _positive_rate(deltas),
        "worsened_rate": _negative_rate(deltas),
        "target_first_rate": hits.count("take_profit") / len(hits) if hits else None,
        "stop_first_rate": hits.count("stop_loss") / len(hits) if hits else None,
        "no_hit_rate": hits.count(None) / len(hits) if hits else None,
    }


def _time_exit_actual_stats(values: list[float]) -> dict[str, Any]:
    return {
        "avg_pnl": _mean_number(values),
        "median_pnl": _median_number(values),
        "win_rate": _positive_rate(values),
    }


def _time_exit_action(windows: dict[str, dict[str, Any]]) -> tuple[int | None, str, list[str]]:
    qualified: list[tuple[int, dict[str, Any]]] = []
    for raw_minutes, stats in windows.items():
        coverage = int(stats.get("coverage") or 0)
        avg_delta = _number(stats.get("avg_delta_vs_actual"))
        improved_rate = _number(stats.get("improved_rate"))
        if coverage >= DARKFLOW_TIME_EXIT_EXTENSION_MIN_SAMPLES and _at_least(avg_delta, DARKFLOW_TIME_EXIT_EXTENSION_MIN_DELTA) and _at_least(improved_rate, DARKFLOW_TIME_EXIT_EXTENSION_MIN_IMPROVED_RATE):
            qualified.append((int(raw_minutes), stats))
    if qualified:
        minutes, stats = max(qualified, key=lambda item: (_number(item[1].get("avg_delta_vs_actual")) or -999.0, _number(item[1].get("improved_rate")) or 0.0))
        return minutes, "extend_with_trailing_stop", [f"{minutes}m 继续持有的平均收益差和改善比例达到延长观察线。", "延长期必须带保护止损，不能裸持。"]
    covered = [stats for stats in windows.values() if int(stats.get("coverage") or 0) >= DARKFLOW_TIME_EXIT_EXTENSION_MIN_SAMPLES]
    harmful = [stats for stats in covered if (_number(stats.get("avg_delta_vs_actual")) or 0.0) < 0]
    if covered and len(harmful) == len(covered):
        return None, "keep_time_exit", ["当前已覆盖窗口继续持有平均表现更差，保留原时间退出。"]
    return None, "collect_more", ["样本或改善幅度不足，继续只读观察。"]


def _time_exit_action_text(action: str) -> str:
    return {
        "extend_with_trailing_stop": "允许延长但必须带保护止损",
        "keep_time_exit": "保持当前时间退出",
        "collect_more": "继续收集复盘样本",
    }.get(action, action)


def _time_exit_row_rank(row: dict[str, Any]) -> tuple[int, int, float]:
    action_rank = {"extend_with_trailing_stop": 3, "collect_more": 2, "keep_time_exit": 1}.get(str(row.get("action")), 0)
    best_window = row.get("best_window_minutes")
    best_stats = row.get("windows", {}).get(str(best_window), {}) if best_window is not None else {}
    return (action_rank, int(row.get("time_exit_trades") or 0), _number(best_stats.get("avg_delta_vs_actual")) or -999.0)


def _mean_number(values: list[float | None]) -> float | None:
    parsed = [float(value) for value in values if value is not None]
    return mean(parsed) if parsed else None


def _median_number(values: list[float | None]) -> float | None:
    parsed = [float(value) for value in values if value is not None]
    return median(parsed) if parsed else None


def _positive_rate(values: list[float | None]) -> float | None:
    parsed = [float(value) for value in values if value is not None]
    return sum(1 for value in parsed if value > 0) / len(parsed) if parsed else None


def _negative_rate(values: list[float | None]) -> float | None:
    parsed = [float(value) for value in values if value is not None]
    return sum(1 for value in parsed if value < 0) / len(parsed) if parsed else None


def _exit_reason_share(counts: dict[str, int], reason: str) -> float | None:
    closed_total = sum(count for key, count in counts.items() if key != "open")
    if closed_total <= 0:
        return None
    return counts.get(reason, 0) / closed_total


def _at_least(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold


def _at_most(value: float | None, threshold: float) -> bool:
    return value is not None and value <= threshold


def _recommendation_confidence(closed: int) -> str:
    if closed >= 30:
        return "high"
    if closed >= 10:
        return "medium"
    return "low"


def _darkflow_recommendation_text(recommendation: str) -> str:
    return {
        "whitelist": "白名单补样",
        "keep_sampling": "继续补样",
        "observe": "继续观察",
        "pause": "暂停补样",
        "blacklist": "黑名单隔离",
    }.get(recommendation, recommendation)


def _darkflow_sampling_action(recommendation: str) -> str:
    return {
        "whitelist": "prioritize",
        "keep_sampling": "prioritize",
        "observe": "watch",
        "pause": "pause",
        "blacklist": "block",
    }.get(recommendation, "watch")


def _darkflow_strategy_action_text(action: str) -> str:
    return {
        "keep": "主路径保留",
        "collect_more": "继续补样",
        "review": "人工复核",
        "deweight": "主路径降权",
    }.get(action, action)


def _darkflow_strategy_weight_multiplier(action: str) -> float:
    return {
        "keep": 1.0,
        "collect_more": 1.0,
        "review": 0.75,
        "deweight": 0.35,
    }.get(action, 1.0)


def _darkflow_recommendation_rank(recommendation: str) -> int:
    return {
        "whitelist": 5,
        "keep_sampling": 4,
        "observe": 3,
        "pause": 2,
        "blacklist": 1,
    }.get(recommendation, 0)


def _darkflow_strategy_action_rank(action: str) -> int:
    return {
        "keep": 4,
        "collect_more": 3,
        "review": 2,
        "deweight": 1,
    }.get(action, 0)


def _trade_stats(trades: list[ShadowPaperTrade]) -> dict[str, Any]:
    stats = summarize_trade_outcomes(
        trades,
        no_loss_profit_factor=999.0,
        drawdown_mode="compound",
    )
    opened_times = [item.opened_at for item in trades if item.opened_at]
    closed_times = [item.closed_at for item in trades if item.status == "closed" and isinstance(item.pnl, (int, float)) and item.closed_at]
    return {
        **stats,
        "gross_win": stats["gross_profit"],
        "gross_loss": stats["gross_loss"],
        "execution_model": _execution_model("mixed"),
        "latest_opened_at": max(opened_times) if opened_times else None,
        "latest_closed_at": max(closed_times) if closed_times else None,
    }


def _unique_plan_trades(trades: list[ShadowPaperTrade]) -> list[ShadowPaperTrade]:
    return unique_shadow_plans(trades, include_horizon=True)


def _grouped_trade_stats(
    trades: list[ShadowPaperTrade],
    *,
    key_func: Any,
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, ...], list[ShadowPaperTrade]] = {}
    for trade in trades:
        buckets.setdefault(key_func(trade), []).append(trade)
    rows = []
    for key, items in buckets.items():
        stats = _trade_stats(items)
        row: dict[str, Any] = {**stats}
        if len(key) == 6:
            row.update(
                {
                    "candidate_type": key[0],
                    "candidate_key": key[1],
                    "symbol": key[2],
                    "timeframe": key[3],
                    "direction": key[4],
                    "horizon": key[5],
                }
            )
        elif len(key) == 1:
            row.update({"horizon": key[0]})
        else:
            row.update({"symbol": key[0], "direction": key[1]})
        rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            row["closed_trades"],
            row["profit_factor"] if row["profit_factor"] is not None else -999.0,
            row["avg_pnl"] if row["avg_pnl"] is not None else -999.0,
            row["total_trades"],
        ),
        reverse=True,
    )


def _exit_reason_counts(trades: list[ShadowPaperTrade]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trade in trades:
        reason = str(trade.exit_reason or "open")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _hold_minutes(trade: ShadowPaperTrade) -> float | None:
    if not trade.opened_at or not trade.closed_at:
        return None
    return max(0.0, (_aware(trade.closed_at) - _aware(trade.opened_at)).total_seconds() / 60.0)


def _median_minutes(values: list[float]) -> float | None:
    if not values:
        return None
    return median(values)


def _candidate_group_key(trade: ShadowPaperTrade) -> tuple[str, ...]:
    return (
        trade.candidate_type,
        trade.candidate_key,
        trade.symbol,
        trade.timeframe,
        trade.direction,
        _trade_horizon(trade),
    )


def _horizon_group_key(trade: ShadowPaperTrade) -> tuple[str]:
    return (_trade_horizon(trade),)


def _symbol_group_key(trade: ShadowPaperTrade) -> tuple[str, str]:
    return (trade.symbol, trade.direction)


def _shadow_candidate_rows(
    report: dict[str, Any],
    *,
    candidate_limit: int,
    include_watchlist: bool,
) -> list[tuple[dict[str, Any], str]]:
    rows: list[tuple[dict[str, Any], str]] = []
    seen: set[str] = set()
    for row in report.get("candidates") or []:
        key = str(row.get("segment_key") or row.get("feature_key") or "")
        if key and key not in seen:
            rows.append((row, "segment_candidate"))
            seen.add(key)
    if include_watchlist:
        for row in report.get("all_segments") or []:
            key = str(row.get("segment_key") or row.get("feature_key") or "")
            if key and key not in seen:
                rows.append((row, "observation_segment"))
                seen.add(key)
    return rows[: max(0, candidate_limit)]


async def _replay_feature_pairs(
    session: AsyncSession,
    *,
    candidate_rows: list[tuple[dict[str, Any], str]],
    horizon: str,
    limit: int,
) -> list[tuple[FeatureEvent, FeatureLabel, dict[str, Any], str]]:
    specs = [_candidate_spec(row, candidate_type) for row, candidate_type in candidate_rows]
    specs = [item for item in specs if item is not None]
    if not specs:
        return []
    filters = [
        and_(
            FeatureEvent.feature_name == spec["feature_name"],
            FeatureEvent.subtype == spec["subtype"],
            FeatureEvent.direction == spec["direction"],
            FeatureEvent.symbol == spec["symbol"],
            FeatureEvent.timeframe == spec["timeframe"],
        )
        for spec in specs
    ]
    query = (
        select(FeatureEvent, FeatureLabel)
        .where(
            FeatureLabel.feature_event_id == FeatureEvent.id,
            FeatureLabel.horizon == horizon,
            FeatureLabel.status == "labeled",
            FeatureLabel.return_pct.is_not(None),
            FeatureEvent.direction.in_(["long", "short"]),
            or_(*filters),
        )
        .order_by(FeatureEvent.event_ts.desc())
        .limit(max(1, limit))
    )
    result = await session.execute(query)
    spec_by_key = {item["segment_key"]: item for item in specs}
    pairs: list[tuple[FeatureEvent, FeatureLabel, dict[str, Any], str]] = []
    for event, label in result.all():
        key = _segment_key_from_event(event)
        spec = spec_by_key.get(key)
        if spec is None:
            continue
        pairs.append((event, label, spec["row"], spec["candidate_type"]))
    return pairs


def _candidate_spec(row: dict[str, Any], candidate_type: str) -> dict[str, Any] | None:
    feature_name = row.get("feature_name")
    subtype = row.get("subtype")
    direction = row.get("direction")
    symbol = row.get("symbol")
    timeframe = row.get("timeframe")
    if not all(isinstance(item, str) and item for item in (feature_name, subtype, direction, symbol, timeframe)):
        return None
    return {
        "feature_name": feature_name,
        "subtype": subtype,
        "direction": direction,
        "symbol": symbol,
        "timeframe": timeframe,
        "segment_key": f"{feature_name}:{subtype}:{direction}:{symbol}:{timeframe}",
        "candidate_type": candidate_type,
        "row": row,
    }


def _segment_key_from_event(event: FeatureEvent) -> str:
    return f"{event.feature_name}:{event.subtype}:{event.direction}:{event.symbol}:{event.timeframe}"


async def _existing_shadow_signal_keys(session: AsyncSession, signal_keys: list[str]) -> set[str]:
    if not signal_keys:
        return set()
    rows = await session.execute(select(ShadowPaperTrade.signal_key).where(ShadowPaperTrade.signal_key.in_(signal_keys)))
    return {str(item) for item in rows.scalars().all()}


def _replay_plan(
    event: FeatureEvent,
    label: FeatureLabel,
    *,
    row: dict[str, Any],
    candidate_type: str,
    source_experiment_run_id: str | None,
    horizon: str,
) -> dict[str, Any] | None:
    return_pct = _number(label.return_pct)
    if return_pct is None:
        return None
    entry_mark = _replay_entry_mark(event, label, return_pct)
    if entry_mark is None or entry_mark <= 0:
        return None
    exit_mark = _replay_exit_mark(event.direction, entry_mark, label, return_pct)
    if exit_mark is None or exit_mark <= 0:
        return None
    asset_tier = event.asset_tier or _asset_tier(event.symbol)
    entry_price = _execution_price(event.direction, entry_mark, side="entry", asset_tier=asset_tier)
    exit_price = _execution_price(event.direction, exit_mark, side="exit", asset_tier=asset_tier)
    levels = _shadow_levels(event.direction, entry_price, asset_tier)
    pnl = _pnl(event.direction, entry_price, exit_price) - SHADOW_FEE_RATE * 2
    stop_pct = abs(entry_price - levels["stop_loss"]) / entry_price if entry_price else 0.0
    candidate_key = str(row.get("segment_key") or _segment_key_from_event(event))
    signal_key = _replay_signal_key(event_id=event.id, label_id=label.id, horizon=horizon, candidate_key=candidate_key)
    opened_at = _aware(event.event_ts)
    closed_at = _aware(label.future_at) if label.future_at else opened_at + FEATURE_HORIZONS[horizon]
    trade = ShadowPaperTrade(
        strategy_name=SHADOW_STRATEGY_NAME,
        candidate_type=candidate_type,
        candidate_key=candidate_key,
        signal_key=signal_key,
        source_experiment_run_id=source_experiment_run_id,
        symbol=event.symbol,
        timeframe=event.timeframe,
        direction=event.direction,
        entry_price=entry_price,
        stop_loss=levels["stop_loss"],
        take_profit=levels["take_profit"],
        position_size=1.0,
        status="closed",
        exit_price=exit_price,
        exit_reason=_replay_exit_reason(pnl),
        pnl=pnl,
        r_multiple=pnl / stop_pct if stop_pct else 0.0,
        mfe=_number(label.mfe) if _number(label.mfe) is not None else pnl,
        mae=_number(label.mae) if _number(label.mae) is not None else pnl,
        opened_at=opened_at,
        closed_at=closed_at,
        context={
            "research_only": True,
            "historical_replay": True,
            "opens_paper_trades": False,
            "opens_live_orders": False,
            "horizon": horizon,
            "feature_event_id": event.id,
            "feature_label_id": label.id,
            "mark_price_at_signal": entry_mark,
            "exit_mark_price": exit_mark,
            "gross_label_return": return_pct,
            "net_pnl_after_cost": pnl,
            "total_fee_rate": SHADOW_FEE_RATE * 2,
            "execution_model": _execution_model(asset_tier),
            "candidate_snapshot": _candidate_context(row),
            "replay_model": "feature_label_horizon_return_with_fee_and_slippage",
        },
    )
    return {"signal_key": signal_key, "trade": trade}


def _replay_entry_mark(event: FeatureEvent, label: FeatureLabel, return_pct: float) -> float | None:
    if isinstance(event.event_price, (int, float)) and event.event_price > 0:
        return float(event.event_price)
    future_price = _number(label.future_price)
    if future_price is None or future_price <= 0:
        return None
    if event.direction == "long":
        denominator = 1 + return_pct
    else:
        denominator = 1 - return_pct
    if denominator <= 0:
        return None
    return future_price / denominator


def _replay_exit_mark(direction: str, entry_mark: float, label: FeatureLabel, return_pct: float) -> float | None:
    future_price = _number(label.future_price)
    if future_price is not None and future_price > 0:
        return future_price
    if direction == "long":
        return entry_mark * (1 + return_pct)
    if direction == "short":
        return entry_mark * (1 - return_pct)
    return None


def _replay_exit_reason(pnl: float) -> str:
    if pnl > 0:
        return "historical_horizon_win"
    if pnl < 0:
        return "historical_horizon_loss"
    return "historical_horizon_flat"


def _replay_signal_key(*, event_id: str, label_id: str, horizon: str, candidate_key: str) -> str:
    raw = f"{SHADOW_STRATEGY_NAME}:historical_replay:{candidate_key}:{event_id}:{label_id}:{horizon}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _latest_price(session: AsyncSession, symbol: str) -> float | None:
    row = await session.scalar(
        select(PriceSnapshot.price).where(PriceSnapshot.symbol == symbol).order_by(PriceSnapshot.created_at.desc()).limit(1)
    )
    return float(row) if isinstance(row, (int, float)) else None


async def _open_shadow_trade(
    session: AsyncSession,
    *,
    candidate_key: str,
    symbol: str,
) -> ShadowPaperTrade | None:
    return await session.scalar(
        select(ShadowPaperTrade)
        .where(
            ShadowPaperTrade.strategy_name == SHADOW_STRATEGY_NAME,
            ShadowPaperTrade.candidate_key == candidate_key,
            ShadowPaperTrade.symbol == symbol,
            ShadowPaperTrade.status == "open",
        )
        .limit(1)
    )


def _shadow_levels(direction: str, entry_price: float, asset_tier: str) -> dict[str, float]:
    template = template_for_tier(asset_tier)
    if direction == "long":
        return {
            "stop_loss": entry_price * (1 - template.stop_pct),
            "take_profit": entry_price * (1 + template.target_pct),
        }
    return {
        "stop_loss": entry_price * (1 + template.stop_pct),
        "take_profit": entry_price * (1 - template.target_pct),
    }


def _execution_price(direction: str, price: float, *, side: str, asset_tier: str) -> float:
    slippage = _slippage_rate(asset_tier)
    if side == "entry":
        worse = 1 + slippage if direction == "long" else 1 - slippage
    else:
        worse = 1 - slippage if direction == "long" else 1 + slippage
    return price * worse


def _net_pnl(trade: ShadowPaperTrade, price: float, *, exit_side: str) -> float:
    gross = _pnl(trade.direction, trade.entry_price, price)
    fee_cost = SHADOW_FEE_RATE if exit_side == "mark" else SHADOW_FEE_RATE * 2
    return gross - fee_cost


def _darkflow_shadow_forward_fallback_mark_price(trade: ShadowPaperTrade) -> tuple[float, str] | None:
    context = trade.context if isinstance(trade.context, dict) else {}
    candidates = (
        (context.get("last_mark_price"), "last_mark_price"),
        (context.get("mark_price_at_signal"), "mark_price_at_signal"),
        (trade.entry_price, "entry_price"),
    )
    for value, source in candidates:
        parsed = _number(value)
        if parsed is not None and parsed > 0:
            return parsed, source
    return None


def _darkflow_shadow_forward_time_exit_context(trade: ShadowPaperTrade, *, now: datetime) -> dict[str, Any] | None:
    context = trade.context if isinstance(trade.context, dict) else {}
    if trade.strategy_name != DARKFLOW_V2_SHADOW_STRATEGY_NAME or context.get("shadow_forward") is not True:
        return None
    deadline, basis = _darkflow_shadow_forward_deadline(trade)
    if deadline is None:
        return None
    now = _aware(now)
    if now <= deadline:
        return None
    opened_at = _aware(trade.opened_at) if isinstance(trade.opened_at, datetime) else None
    payload: dict[str, Any] = {
        "shadow_forward_time_exit": True,
        "time_exit_basis": basis,
        "max_hold_until": deadline.isoformat(),
        "time_exit_checked_at": now.isoformat(),
    }
    if opened_at is not None:
        payload["hold_seconds"] = max(0.0, (deadline - opened_at).total_seconds())
        payload["age_seconds_at_time_exit"] = max(0.0, (now - opened_at).total_seconds())
    return payload


def _darkflow_shadow_forward_deadline(trade: ShadowPaperTrade) -> tuple[datetime | None, str | None]:
    context = trade.context if isinstance(trade.context, dict) else {}
    entry_plan_state = context.get("entry_plan_state") if isinstance(context.get("entry_plan_state"), dict) else {}
    valid_until = _parse_iso_datetime(entry_plan_state.get("valid_until"))
    if valid_until is not None:
        return valid_until, "entry_plan_valid_until"
    opened_at = _aware(trade.opened_at) if isinstance(trade.opened_at, datetime) else None
    if opened_at is None:
        return None, None
    snapshot = context.get("candidate_snapshot") if isinstance(context.get("candidate_snapshot"), dict) else {}
    interval = str(snapshot.get("interval") or context.get("horizon") or "").strip().lower()
    return opened_at + _darkflow_shadow_forward_max_hold(interval, trade.timeframe), "interval_fallback"


def _darkflow_shadow_forward_max_hold(interval: str | None, timeframe: str | None) -> timedelta:
    interval_delta = _interval_delta(interval)
    if interval_delta is not None:
        return max(timedelta(hours=2), min(interval_delta * 12, timedelta(hours=72)))
    raw_timeframe = str(timeframe or "").lower()
    if raw_timeframe in {"long", "daily", "4h", "24h"}:
        return timedelta(hours=24)
    if raw_timeframe in {"mid", "1h"}:
        return timedelta(hours=12)
    return timedelta(hours=6)


def _interval_delta(value: str | None) -> timedelta | None:
    raw = str(value or "").strip().lower()
    if len(raw) < 2 or not raw[:-1].isdigit():
        return None
    count = int(raw[:-1])
    unit = raw[-1]
    if count <= 0:
        return None
    if unit == "m":
        return timedelta(minutes=count)
    if unit == "h":
        return timedelta(hours=count)
    if unit == "d":
        return timedelta(days=count)
    return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _slippage_rate(asset_tier: str) -> float:
    return SHADOW_SLIPPAGE_RATE_BY_TIER.get(asset_tier, SHADOW_SLIPPAGE_RATE_BY_TIER["high_volatility"])


def _execution_model(asset_tier: str) -> dict[str, Any]:
    return {
        "fee_rate_per_side": SHADOW_FEE_RATE,
        "round_trip_fee_rate": SHADOW_FEE_RATE * 2,
        "slippage_rate": _slippage_rate(asset_tier),
        "entry_and_exit_use_worse_price": True,
        "mode": "conservative_shadow_paper",
    }


def _merge_context(current: dict[str, Any] | None, updates: dict[str, Any]) -> dict[str, Any]:
    payload = dict(current or {})
    execution_model = payload.get("execution_model") if isinstance(payload.get("execution_model"), dict) else {}
    payload.update(updates)
    if execution_model and "execution_model" not in updates:
        payload["execution_model"] = execution_model
    return payload


def _max_drawdown(returns: list[float]) -> float:
    return max_drawdown(returns)


def _promotion_report(candidate_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for row in candidate_rows:
        status, blockers = _promotion_status(row)
        rows.append({**row, "promotion_status": status, "promotion_blockers": blockers})
    return {
        "criteria": {
            "min_closed_trades": PROMOTION_MIN_CLOSED_TRADES,
            "min_win_rate": PROMOTION_MIN_WIN_RATE,
            "min_profit_factor": PROMOTION_MIN_PROFIT_FACTOR,
            "max_drawdown": PROMOTION_MAX_DRAWDOWN,
            "cost_model_required": True,
        },
        "ready": [row for row in rows if row["promotion_status"] == "ready_for_paper_weight"],
        "edge_unstable": [row for row in rows if row["promotion_status"] == "edge_unstable_drawdown"],
        "watchlist": [row for row in rows if row["promotion_status"] == "watchlist"],
        "rejected": [row for row in rows if row["promotion_status"] == "reject_or_pause"],
        "all": rows,
    }


def _promotion_status(row: dict[str, Any]) -> tuple[str, list[str]]:
    blockers: list[str] = []
    closed = int(row.get("closed_trades") or 0)
    win_rate = row.get("win_rate")
    profit_factor = row.get("profit_factor")
    max_drawdown = float(row.get("max_drawdown") or 0.0)
    if closed < PROMOTION_MIN_CLOSED_TRADES:
        blockers.append("insufficient_closed_trades")
    if not isinstance(win_rate, (int, float)) or float(win_rate) < PROMOTION_MIN_WIN_RATE:
        blockers.append("win_rate_below_threshold")
    if not isinstance(profit_factor, (int, float)) or float(profit_factor) < PROMOTION_MIN_PROFIT_FACTOR:
        blockers.append("profit_factor_below_threshold")
    if max_drawdown > PROMOTION_MAX_DRAWDOWN:
        blockers.append("drawdown_above_threshold")
    if not blockers:
        return "ready_for_paper_weight", []
    if _has_positive_edge_but_unstable_drawdown(row, blockers):
        return "edge_unstable_drawdown", blockers
    if closed >= PROMOTION_MIN_CLOSED_TRADES:
        return "reject_or_pause", blockers
    if closed >= max(5, PROMOTION_MIN_CLOSED_TRADES // 3) and blockers != ["insufficient_closed_trades"]:
        return "watchlist", blockers
    return "observing", blockers


def _has_positive_edge_but_unstable_drawdown(row: dict[str, Any], blockers: list[str]) -> bool:
    if set(blockers) != {"drawdown_above_threshold"}:
        return False
    closed = int(row.get("closed_trades") or 0)
    avg_pnl = row.get("avg_pnl")
    return closed >= PROMOTION_MIN_CLOSED_TRADES and isinstance(avg_pnl, (int, float)) and float(avg_pnl) > 0


def _asset_tier(symbol: str) -> str:
    coin = symbol.removesuffix("USDT")
    if coin in {"BTC", "ETH"}:
        return "core"
    if coin in {"SOL", "BNB", "LINK", "TON"}:
        return "mainstream"
    return "high_volatility"


def _signal_key(*, candidate_key: str, symbol: str, direction: str, price: float) -> str:
    raw = f"{SHADOW_STRATEGY_NAME}:{candidate_key}:{symbol}:{direction}:{round(price, 6)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _shadow_runner_decision(trade: ShadowPaperTrade, price: float) -> dict[str, Any]:
    if not _runner_asset_eligible(trade):
        return _runner_decision(False, blockers=["asset_not_runner_eligible"])
    if trade.entry_price <= 0 or price <= 0:
        return _runner_decision(False, blockers=["invalid_price"])
    if trade.direction == "long" and price <= trade.entry_price:
        return _runner_decision(False, blockers=["not_profitable"])
    if trade.direction == "short" and price >= trade.entry_price:
        return _runner_decision(False, blockers=["not_profitable"])

    evidence = _shadow_runner_evidence(trade)
    return _runner_decision(
        _runner_extension_allowed(evidence),
        score=evidence["score"],
        signals=evidence["signals"],
        blockers=evidence["blockers"],
    )


def _shadow_runner_evidence(trade: ShadowPaperTrade) -> dict[str, Any]:
    context = trade.context or {}
    candidate = context.get("candidate_snapshot") if isinstance(context.get("candidate_snapshot"), dict) else {}
    key_blob = " ".join(
        str(value or "")
        for value in (
            trade.candidate_key,
            candidate.get("segment_key"),
            candidate.get("feature_key"),
            candidate.get("promotion_status"),
        )
    ).lower()
    sample_count = _number(candidate.get("sample_count")) or 0.0
    raw_sample_count = _number(candidate.get("raw_sample_count")) or 0.0
    win_rate = _number(candidate.get("win_rate"))
    profit_factor = _number(candidate.get("profit_factor"))
    reliability_score = _number(candidate.get("reliability_score"))
    avg_return = _number(candidate.get("avg_return"))

    trend_feature = any(token in key_blob for token in ("trend", "smart_money", "inst_vwap", "micro_poc", "poc", "volume_profile", "hvn"))
    liquidity_feature = any(token in key_blob for token in ("liq", "liquidation", "sweep", "heatmap", "stop_loss"))
    orderflow_feature = any(token in key_blob for token in ("cross_exchange", "imbalance", "orderflow", "ob_decay", "order_blocks"))
    performance_support = (
        (profit_factor is not None and profit_factor >= 1.1)
        or (win_rate is not None and win_rate >= 0.52)
        or (avg_return is not None and avg_return > 0)
    )
    statistical_support = sample_count >= 10 or raw_sample_count >= 30 or (reliability_score is not None and reliability_score >= 0.1)
    signals = {
        "same_direction": True,
        "fresh": True,
        "score_above_minimum": statistical_support,
        "execution_ready": performance_support,
        "dark_flow_target": liquidity_feature or orderflow_feature,
        "trend_aligned": trend_feature,
        "liquidity_context": liquidity_feature,
        "orderflow_confirmed": orderflow_feature,
        "exhaustion_filter_present": "exhaustion" in key_blob,
    }
    blockers: list[str] = []
    if not statistical_support:
        blockers.append("insufficient_shadow_candidate_support")
    if not performance_support:
        blockers.append("shadow_candidate_performance_weak")
    if not (trend_feature or liquidity_feature or orderflow_feature):
        blockers.append("missing_continuation_feature")
    if not (liquidity_feature or orderflow_feature):
        blockers.append("missing_flow_confirmation")
    score = (
        1.5
        + 1.0
        + (1.0 if signals["score_above_minimum"] else 0.0)
        + (1.25 if signals["execution_ready"] else 0.0)
        + (1.25 if signals["dark_flow_target"] else 0.0)
        + (1.25 if signals["trend_aligned"] else 0.0)
        + (0.9 if signals["liquidity_context"] else 0.0)
        + (0.9 if signals["orderflow_confirmed"] else 0.0)
        + (0.4 if signals["exhaustion_filter_present"] else 0.0)
    )
    return {"score": round(score, 3), "signals": signals, "blockers": blockers}


def _candidate_context(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_key": row.get("segment_key"),
        "feature_key": row.get("feature_key"),
        "symbol": row.get("symbol"),
        "timeframe": row.get("timeframe"),
        "direction": row.get("direction"),
        "sample_count": row.get("sample_count"),
        "raw_sample_count": row.get("raw_sample_count"),
        "win_rate": row.get("win_rate"),
        "win_rate_lower": row.get("win_rate_lower"),
        "avg_return": row.get("avg_return"),
        "avg_return_lower": row.get("avg_return_lower"),
        "profit_factor": row.get("profit_factor"),
        "profit_factor_lower": row.get("profit_factor_lower"),
        "reliability_score": row.get("reliability_score"),
        "time_split": row.get("time_split"),
        "overfit_risk": row.get("overfit_risk"),
        "promotion_status": row.get("promotion_status"),
        "rejection_reasons": row.get("rejection_reasons"),
    }


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _trade_payload(trade: ShadowPaperTrade) -> dict[str, Any]:
    return {
        "id": trade.id,
        "strategy_name": trade.strategy_name,
        "candidate_type": trade.candidate_type,
        "candidate_key": trade.candidate_key,
        "symbol": trade.symbol,
        "timeframe": trade.timeframe,
        "direction": trade.direction,
        "entry_price": trade.entry_price,
        "stop_loss": trade.stop_loss,
        "take_profit": trade.take_profit,
        "status": trade.status,
        "exit_price": trade.exit_price,
        "exit_reason": trade.exit_reason,
        "pnl": trade.pnl,
        "r_multiple": trade.r_multiple,
        "mfe": trade.mfe,
        "mae": trade.mae,
        "outcome": build_trade_outcome(trade, source="shadow"),
        "opened_at": trade.opened_at,
        "closed_at": trade.closed_at,
        "source_experiment_run_id": trade.source_experiment_run_id,
        "context": trade.context,
    }


def _shadow_policy() -> dict[str, Any]:
    return {
        "research_only": True,
        "opens_paper_trades": False,
        "opens_live_orders": False,
        "sends_entry_notifications": False,
        "lineage": legacy_feature_research_lineage(),
        "isolated_table": "shadow_paper_trades",
        "uses_fee_and_slippage": True,
        "default_execution_model": _execution_model("mixed"),
    }
