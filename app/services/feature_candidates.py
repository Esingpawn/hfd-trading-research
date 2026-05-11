from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CollectionRun, ExperimentRun, FeatureEvent as FeatureEventModel, FeatureLabel, SignalSnapshot
from app.services.features import FEATURE_HORIZONS


DEFAULT_MIN_SAMPLES = 30
DEFAULT_MIN_WIN_RATE = 0.52
DEFAULT_MIN_PROFIT_FACTOR = 1.2
DEFAULT_MIN_AVG_RETURN = 0.0
DEFAULT_SEGMENT_MIN_SAMPLES = 5
DEFAULT_MIN_SEGMENTS = 2
DEFAULT_DEDUPE_RESEARCH_SAMPLES = True
DEFAULT_DEDUPE_BUCKET_MINUTES = 30
DEFAULT_MIN_UNIQUE_TIME_BUCKETS = 3
DEFAULT_MIN_UNIQUE_EVENT_DAYS = 2
DEFAULT_MIN_UNIQUE_MARKET_WINDOWS = 2
DEFAULT_MIN_UNIQUE_COLLECTION_RUNS = 2
DEFAULT_MARKET_WINDOW_HOURS = 8
DEFAULT_MAX_SAME_RETURN_SAMPLES = 10
DEFAULT_MAX_RETURN_CLUSTER_RATIO = 0.75
DEFAULT_BALANCED_SAMPLE_DAYS = 14


FeaturePair = tuple[FeatureEventModel, FeatureLabel, SignalSnapshot | None, CollectionRun | None]


async def feature_candidate_screen(
    session: AsyncSession,
    *,
    horizon: str = "30m",
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_win_rate: float = DEFAULT_MIN_WIN_RATE,
    min_profit_factor: float = DEFAULT_MIN_PROFIT_FACTOR,
    min_avg_return: float = DEFAULT_MIN_AVG_RETURN,
    segment_min_samples: int = DEFAULT_SEGMENT_MIN_SAMPLES,
    min_segments: int = DEFAULT_MIN_SEGMENTS,
    limit: int = 20000,
    persist: bool = False,
) -> dict[str, Any]:
    _validate_horizon(horizon)
    thresholds = _thresholds(
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_avg_return=min_avg_return,
        segment_min_samples=segment_min_samples,
        min_segments=min_segments,
    )
    pairs = await _labeled_feature_pairs(session, horizon=horizon, limit=limit)
    rows = _candidate_rows(pairs, thresholds=thresholds)
    candidates = [row for row in rows if row["paper_ab_ready"]]
    watchlist = [row for row in rows if row["promotion_status"] == "watchlist"]
    report: dict[str, Any] = {
        "horizon": horizon,
        "limit": limit,
        "labeled_count": len(pairs),
        "feature_group_count": len(rows),
        "candidate_count": len(candidates),
        "watchlist_count": len(watchlist),
        "thresholds": thresholds,
        "policy": _research_policy(),
        "candidates": candidates,
        "watchlist": watchlist,
        "rejected_summary": _rejection_summary(rows),
        "all_features": rows,
    }
    if persist:
        report["experiment_run"] = await _persist_experiment(
            session,
            name=f"feature_candidates_{horizon}",
            scope={"horizon": horizon, "limit": limit, "labeled_count": len(pairs)},
            params=thresholds,
            metrics=report,
            notes="Research-only candidate feature screening; does not affect strategy scoring or trading.",
        )
    return report


async def feature_paper_ab(
    session: AsyncSession,
    *,
    horizon: str = "30m",
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_win_rate: float = DEFAULT_MIN_WIN_RATE,
    min_profit_factor: float = DEFAULT_MIN_PROFIT_FACTOR,
    min_avg_return: float = DEFAULT_MIN_AVG_RETURN,
    segment_min_samples: int = DEFAULT_SEGMENT_MIN_SAMPLES,
    min_segments: int = DEFAULT_MIN_SEGMENTS,
    candidate_limit: int = 20,
    limit: int = 20000,
    persist: bool = False,
) -> dict[str, Any]:
    candidate_report = await feature_candidate_screen(
        session,
        horizon=horizon,
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_avg_return=min_avg_return,
        segment_min_samples=segment_min_samples,
        min_segments=min_segments,
        limit=limit,
        persist=False,
    )
    selected = candidate_report["candidates"][:candidate_limit]
    selected_keys = {str(row["feature_key"]) for row in selected}
    pairs = await _labeled_feature_pairs(session, horizon=horizon, limit=limit)
    candidate_pairs = [item for item in pairs if _feature_key(item[0]) in selected_keys]
    control_pairs = [item for item in pairs if _feature_key(item[0]) not in selected_keys]
    candidate_stats = _pseudo_trade_stats(candidate_pairs)
    control_stats = _pseudo_trade_stats(control_pairs)
    report: dict[str, Any] = {
        "horizon": horizon,
        "limit": limit,
        "candidate_limit": candidate_limit,
        "selected_candidate_count": len(selected),
        "selected_feature_keys": [row["feature_key"] for row in selected],
        "thresholds": candidate_report["thresholds"],
        "policy": _research_policy(),
        "data_quality": {
            "labeled_count": len(pairs),
            "candidate_pseudo_trade_count": candidate_stats["trade_count"],
            "control_pseudo_trade_count": control_stats["trade_count"],
            "status": "ready" if selected else "no_candidate_features",
        },
        "arms": {
            "candidate": candidate_stats,
            "control": control_stats,
            "edge": _arm_edge(candidate_stats, control_stats),
        },
        "per_candidate": [
            {
                **row,
                "pseudo_trade_metrics": _pseudo_trade_stats(
                    [item for item in candidate_pairs if _feature_key(item[0]) == row["feature_key"]]
                ),
            }
            for row in selected
        ],
        "candidate_screen": {
            "candidate_count": candidate_report["candidate_count"],
            "watchlist_count": candidate_report["watchlist_count"],
            "rejected_summary": candidate_report["rejected_summary"],
        },
    }
    if persist:
        report["experiment_run"] = await _persist_experiment(
            session,
            name=f"feature_paper_ab_{horizon}",
            scope={"horizon": horizon, "limit": limit, "candidate_limit": candidate_limit},
            params=candidate_report["thresholds"],
            metrics=report,
            notes="Report-only paper A/B using feature labels as pseudo-trades; no PaperTrade rows are opened.",
        )
    return report


async def feature_segment_candidate_screen(
    session: AsyncSession,
    *,
    horizon: str = "30m",
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_win_rate: float = DEFAULT_MIN_WIN_RATE,
    min_profit_factor: float = DEFAULT_MIN_PROFIT_FACTOR,
    min_avg_return: float = DEFAULT_MIN_AVG_RETURN,
    dedupe_research_samples: bool = DEFAULT_DEDUPE_RESEARCH_SAMPLES,
    dedupe_bucket_minutes: int = DEFAULT_DEDUPE_BUCKET_MINUTES,
    min_unique_time_buckets: int = DEFAULT_MIN_UNIQUE_TIME_BUCKETS,
    min_unique_event_days: int = DEFAULT_MIN_UNIQUE_EVENT_DAYS,
    min_unique_market_windows: int = DEFAULT_MIN_UNIQUE_MARKET_WINDOWS,
    min_unique_collection_runs: int = DEFAULT_MIN_UNIQUE_COLLECTION_RUNS,
    market_window_hours: int = DEFAULT_MARKET_WINDOW_HOURS,
    max_same_return_samples: int = DEFAULT_MAX_SAME_RETURN_SAMPLES,
    max_return_cluster_ratio: float = DEFAULT_MAX_RETURN_CLUSTER_RATIO,
    limit: int = 20000,
    persist: bool = False,
) -> dict[str, Any]:
    _validate_horizon(horizon)
    thresholds = _thresholds(
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_avg_return=min_avg_return,
        segment_min_samples=min_samples,
        min_segments=1,
        dedupe_research_samples=dedupe_research_samples,
        dedupe_bucket_minutes=dedupe_bucket_minutes,
        min_unique_time_buckets=min_unique_time_buckets,
        min_unique_event_days=min_unique_event_days,
        min_unique_market_windows=min_unique_market_windows,
        min_unique_collection_runs=min_unique_collection_runs,
        market_window_hours=market_window_hours,
        max_same_return_samples=max_same_return_samples,
        max_return_cluster_ratio=max_return_cluster_ratio,
    )
    pairs = await _labeled_feature_pairs(session, horizon=horizon, limit=limit)
    rows = _segment_candidate_rows(pairs, thresholds=thresholds)
    candidates = [row for row in rows if row["paper_ab_ready"]]
    report: dict[str, Any] = {
        "horizon": horizon,
        "limit": limit,
        "labeled_count": len(pairs),
        "segment_group_count": len(rows),
        "candidate_count": len(candidates),
        "thresholds": thresholds,
        "policy": _research_policy(),
        "quality_summary": _research_quality_summary(rows),
        "risk_summary": _risk_summary(rows),
        "candidates": candidates,
        "rejected_summary": _rejection_summary(rows),
        "by_feature": _segment_candidate_feature_summary(candidates),
        "all_segments": rows,
    }
    if persist:
        report["experiment_run"] = await _persist_experiment(
            session,
            name=f"feature_segment_candidates_{horizon}",
            scope={"horizon": horizon, "limit": limit, "labeled_count": len(pairs)},
            params=thresholds,
            metrics=report,
            notes="Research-only segment-aware feature screening; does not affect strategy scoring or trading.",
        )
    return report


async def feature_segment_paper_ab(
    session: AsyncSession,
    *,
    horizon: str = "30m",
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_win_rate: float = DEFAULT_MIN_WIN_RATE,
    min_profit_factor: float = DEFAULT_MIN_PROFIT_FACTOR,
    min_avg_return: float = DEFAULT_MIN_AVG_RETURN,
    dedupe_research_samples: bool = DEFAULT_DEDUPE_RESEARCH_SAMPLES,
    dedupe_bucket_minutes: int = DEFAULT_DEDUPE_BUCKET_MINUTES,
    min_unique_time_buckets: int = DEFAULT_MIN_UNIQUE_TIME_BUCKETS,
    min_unique_event_days: int = DEFAULT_MIN_UNIQUE_EVENT_DAYS,
    min_unique_market_windows: int = DEFAULT_MIN_UNIQUE_MARKET_WINDOWS,
    min_unique_collection_runs: int = DEFAULT_MIN_UNIQUE_COLLECTION_RUNS,
    market_window_hours: int = DEFAULT_MARKET_WINDOW_HOURS,
    max_same_return_samples: int = DEFAULT_MAX_SAME_RETURN_SAMPLES,
    max_return_cluster_ratio: float = DEFAULT_MAX_RETURN_CLUSTER_RATIO,
    candidate_limit: int = 50,
    limit: int = 20000,
    persist: bool = False,
) -> dict[str, Any]:
    segment_report = await feature_segment_candidate_screen(
        session,
        horizon=horizon,
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_avg_return=min_avg_return,
        dedupe_research_samples=dedupe_research_samples,
        dedupe_bucket_minutes=dedupe_bucket_minutes,
        min_unique_time_buckets=min_unique_time_buckets,
        min_unique_event_days=min_unique_event_days,
        min_unique_market_windows=min_unique_market_windows,
        min_unique_collection_runs=min_unique_collection_runs,
        market_window_hours=market_window_hours,
        max_same_return_samples=max_same_return_samples,
        max_return_cluster_ratio=max_return_cluster_ratio,
        limit=limit,
        persist=False,
    )
    thresholds = segment_report["thresholds"]
    selected = segment_report["candidates"][:candidate_limit]
    selected_keys = {str(row["segment_key"]) for row in selected}
    selected_symbol_timeframes = {str(row["symbol_timeframe"]) for row in selected}
    pairs = await _labeled_feature_pairs(session, horizon=horizon, limit=limit)
    raw_candidate_pairs = [item for item in pairs if _segment_key(item[0]) in selected_keys]
    raw_matched_control_pairs = [
        item
        for item in pairs
        if _symbol_timeframe(item[0]) in selected_symbol_timeframes and _segment_key(item[0]) not in selected_keys
    ]
    raw_all_control_pairs = [item for item in pairs if _segment_key(item[0]) not in selected_keys]
    candidate_pairs = _dedupe_research_pairs(raw_candidate_pairs, thresholds=thresholds, key_func=_segment_key)
    matched_control_pairs = _dedupe_research_pairs(raw_matched_control_pairs, thresholds=thresholds, key_func=_segment_key)
    all_control_pairs = _dedupe_research_pairs(raw_all_control_pairs, thresholds=thresholds, key_func=_segment_key)
    candidate_stats = _pseudo_trade_stats(candidate_pairs)
    matched_control_stats = _pseudo_trade_stats(matched_control_pairs)
    all_control_stats = _pseudo_trade_stats(all_control_pairs)
    report: dict[str, Any] = {
        "horizon": horizon,
        "limit": limit,
        "candidate_limit": candidate_limit,
        "selected_candidate_count": len(selected),
        "selected_segment_keys": [row["segment_key"] for row in selected],
        "thresholds": thresholds,
        "policy": _research_policy(),
        "data_quality": {
            "labeled_count": len(pairs),
            "candidate_pseudo_trade_count": candidate_stats["trade_count"],
            "raw_candidate_pseudo_trade_count": len(raw_candidate_pairs),
            "matched_control_pseudo_trade_count": matched_control_stats["trade_count"],
            "raw_matched_control_pseudo_trade_count": len(raw_matched_control_pairs),
            "all_control_pseudo_trade_count": all_control_stats["trade_count"],
            "raw_all_control_pseudo_trade_count": len(raw_all_control_pairs),
            "status": "ready" if selected else "no_segment_candidate_features",
        },
        "quality": {
            "candidate": _sample_quality(raw_candidate_pairs, candidate_pairs, thresholds=thresholds),
            "matched_control": _sample_quality(
                raw_matched_control_pairs, matched_control_pairs, thresholds=thresholds
            ),
            "all_control": _sample_quality(raw_all_control_pairs, all_control_pairs, thresholds=thresholds),
        },
        "arms": {
            "candidate": candidate_stats,
            "matched_control": matched_control_stats,
            "all_control": all_control_stats,
            "matched_edge": _arm_edge(candidate_stats, matched_control_stats),
            "all_edge": _arm_edge(candidate_stats, all_control_stats),
        },
        "per_segment": [
            {
                **row,
                "pseudo_trade_metrics": _pseudo_trade_stats(
                    _dedupe_research_pairs(
                        [
                            item
                            for item in raw_candidate_pairs
                            if _segment_key(item[0]) == row["segment_key"]
                        ],
                        thresholds=thresholds,
                        key_func=_segment_key,
                    )
                ),
            }
            for row in selected
        ],
        "segment_screen": {
            "candidate_count": segment_report["candidate_count"],
            "rejected_summary": segment_report["rejected_summary"],
            "by_feature": segment_report["by_feature"],
        },
    }
    if persist:
        report["experiment_run"] = await _persist_experiment(
            session,
            name=f"feature_segment_paper_ab_{horizon}",
            scope={"horizon": horizon, "limit": limit, "candidate_limit": candidate_limit},
            params=thresholds,
            metrics=report,
            notes="Report-only segment-aware paper A/B using feature labels as pseudo-trades; no PaperTrade rows are opened.",
        )
    return report


async def _labeled_feature_pairs(
    session: AsyncSession,
    *,
    horizon: str,
    limit: int,
) -> list[FeaturePair]:
    row_items = await _balanced_labeled_feature_rows(session, horizon=horizon, limit=limit)
    collection_runs = await _collection_run_windows(session)
    return [
        (event, label, snapshot, collection_run or _infer_collection_run(snapshot, collection_runs))
        for event, label, snapshot, collection_run in row_items
        if isinstance(label.return_pct, (int, float)) and event.direction in {"long", "short"}
    ]


async def _balanced_labeled_feature_rows(
    session: AsyncSession,
    *,
    horizon: str,
    limit: int,
) -> list[tuple[FeatureEventModel, FeatureLabel, SignalSnapshot | None, CollectionRun | None]]:
    if limit <= 0:
        return []
    days = await _labeled_event_days(session, horizon=horizon, limit=min(limit, DEFAULT_BALANCED_SAMPLE_DAYS))
    if not days:
        return []
    per_day_limit = max(1, (limit + len(days) - 1) // len(days))
    rows: list[tuple[FeatureEventModel, FeatureLabel, SignalSnapshot | None, CollectionRun | None]] = []
    seen_event_ids: set[str] = set()
    day_expr = func.date(FeatureEventModel.event_ts)
    for event_day in days:
        result = await session.execute(
            _labeled_feature_query(horizon)
            .where(day_expr == event_day)
            .order_by(FeatureEventModel.event_ts.desc())
            .limit(per_day_limit)
        )
        _append_unique_feature_rows(rows, result.all(), seen_event_ids=seen_event_ids)
    if len(rows) < limit:
        fill_query = _labeled_feature_query(horizon).order_by(FeatureEventModel.event_ts.desc()).limit(limit - len(rows))
        if seen_event_ids:
            fill_query = fill_query.where(~FeatureEventModel.id.in_(seen_event_ids))
        result = await session.execute(fill_query)
        _append_unique_feature_rows(rows, result.all(), seen_event_ids=seen_event_ids)
    return sorted(rows, key=lambda row: _aware(row[0].event_ts), reverse=True)[:limit]


async def _labeled_event_days(
    session: AsyncSession,
    *,
    horizon: str,
    limit: int,
) -> list[Any]:
    day_expr = func.date(FeatureEventModel.event_ts)
    rows = await session.execute(
        select(day_expr)
        .where(
            FeatureLabel.feature_event_id == FeatureEventModel.id,
            FeatureLabel.horizon == horizon,
            FeatureLabel.status == "labeled",
        )
        .group_by(day_expr)
        .order_by(day_expr.desc())
        .limit(limit)
    )
    return [item for item in rows.scalars().all() if item is not None]


def _labeled_feature_query(horizon: str):
    return (
        select(FeatureEventModel, FeatureLabel, SignalSnapshot, CollectionRun)
        .outerjoin(SignalSnapshot, SignalSnapshot.id == FeatureEventModel.snapshot_id)
        .outerjoin(CollectionRun, CollectionRun.id == SignalSnapshot.collection_run_id)
        .where(
            FeatureLabel.feature_event_id == FeatureEventModel.id,
            FeatureLabel.horizon == horizon,
            FeatureLabel.status == "labeled",
        )
    )


def _append_unique_feature_rows(
    rows: list[tuple[FeatureEventModel, FeatureLabel, SignalSnapshot | None, CollectionRun | None]],
    candidates: list[tuple[FeatureEventModel, FeatureLabel, SignalSnapshot | None, CollectionRun | None]],
    *,
    seen_event_ids: set[str],
) -> None:
    for item in candidates:
        event = item[0]
        if event.id in seen_event_ids:
            continue
        seen_event_ids.add(event.id)
        rows.append(item)


async def _collection_run_windows(session: AsyncSession) -> list[CollectionRun]:
    rows = await session.execute(select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(5000))
    return list(rows.scalars().all())


def _infer_collection_run(snapshot: SignalSnapshot | None, collection_runs: list[CollectionRun]) -> CollectionRun | None:
    if snapshot is None:
        return None
    collected_at = _aware(snapshot.collected_at)
    for run in collection_runs:
        started_at = _aware(run.started_at)
        finished_at = _aware(run.finished_at) if run.finished_at else started_at + timedelta(minutes=30)
        if started_at - timedelta(seconds=5) <= collected_at <= finished_at + timedelta(seconds=5):
            return run
    return None


def _candidate_rows(
    pairs: list[FeaturePair],
    *,
    thresholds: dict[str, Any],
) -> list[dict[str, Any]]:
    buckets: dict[str, list[FeaturePair]] = {}
    for item in pairs:
        buckets.setdefault(_feature_key(item[0]), []).append(item)
    rows = []
    for feature_key, items in buckets.items():
        stats = _pseudo_trade_stats(items)
        first = items[0][0]
        segment_report = _segment_report(items, thresholds=thresholds)
        reasons = _candidate_reasons(stats, segment_report, thresholds=thresholds)
        promotion_status = _promotion_status(reasons)
        symbols = sorted({event.symbol for event, _label, _snapshot, _run in items})
        timeframes = sorted({event.timeframe for event, _label, _snapshot, _run in items})
        rows.append(
            {
                "feature_key": feature_key,
                "indicator": first.indicator,
                "feature_name": first.feature_name,
                "subtype": first.subtype,
                "direction": first.direction,
                "symbols": symbols[:12],
                "timeframes": timeframes[:12],
                "symbol_count": len(symbols),
                "timeframe_count": len(timeframes),
                "sample_count": stats["trade_count"],
                "win_rate": stats["win_rate"],
                "avg_return": stats["avg_return"],
                "median_return": stats["median_return"],
                "profit_factor": stats["profit_factor"],
                "avg_mfe": stats["avg_mfe"],
                "avg_mae": stats["avg_mae"],
                "avg_strength": _avg_strength(items),
                "segment_count": segment_report["segment_count"],
                "weak_segments": segment_report["weak_segments"],
                "rejection_reasons": reasons,
                "promotion_status": promotion_status,
                "paper_ab_ready": promotion_status == "candidate",
                "used_for_execution_weights": False,
                "used_for_opening_decisions": False,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            row["paper_ab_ready"],
            row["promotion_status"] == "watchlist",
            row["avg_return"] if row["avg_return"] is not None else -999.0,
            row["win_rate"] if row["win_rate"] is not None else 0.0,
            row["sample_count"],
        ),
        reverse=True,
    )


def _segment_candidate_rows(
    pairs: list[FeaturePair],
    *,
    thresholds: dict[str, Any],
) -> list[dict[str, Any]]:
    buckets: dict[str, list[FeaturePair]] = {}
    for item in pairs:
        buckets.setdefault(_segment_key(item[0]), []).append(item)
    rows = []
    for segment_key, items in buckets.items():
        effective_items = _dedupe_research_pairs(items, thresholds=thresholds, key_func=_segment_key)
        raw_stats = _pseudo_trade_stats(items)
        stats = _pseudo_trade_stats(effective_items)
        quality = _sample_quality(items, effective_items, thresholds=thresholds)
        first = items[0][0]
        reasons = _segment_candidate_reasons(stats, quality=quality, thresholds=thresholds)
        promotion_status = "segment_candidate" if not reasons else "rejected"
        rows.append(
            {
                "segment_key": segment_key,
                "feature_key": _feature_key(first),
                "symbol_timeframe": _symbol_timeframe(first),
                "indicator": first.indicator,
                "feature_name": first.feature_name,
                "subtype": first.subtype,
                "direction": first.direction,
                "symbol": first.symbol,
                "timeframe": first.timeframe,
                "sample_count": stats["trade_count"],
                "raw_sample_count": raw_stats["trade_count"],
                "win_rate": stats["win_rate"],
                "avg_return": stats["avg_return"],
                "median_return": stats["median_return"],
                "profit_factor": stats["profit_factor"],
                "avg_mfe": stats["avg_mfe"],
                "avg_mae": stats["avg_mae"],
                "avg_strength": _avg_strength(effective_items),
                "quality": quality,
                "overfit_risk": quality["overfit_risk"],
                "rejection_reasons": reasons,
                "promotion_status": promotion_status,
                "paper_ab_ready": promotion_status == "segment_candidate",
                "used_for_execution_weights": False,
                "used_for_opening_decisions": False,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            row["paper_ab_ready"],
            row["avg_return"] if row["avg_return"] is not None else -999.0,
            row["win_rate"] if row["win_rate"] is not None else 0.0,
            row["sample_count"],
        ),
        reverse=True,
    )


def _segment_report(
    items: list[FeaturePair],
    *,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    buckets: dict[str, list[FeaturePair]] = {}
    for item in items:
        event = item[0]
        buckets.setdefault(f"{event.symbol}:{event.timeframe}", []).append(item)
    weak_segments = []
    eligible_count = 0
    for name, segment_items in buckets.items():
        stats = _pseudo_trade_stats(segment_items)
        if stats["trade_count"] < thresholds["segment_min_samples"]:
            continue
        eligible_count += 1
        profit_factor = stats["profit_factor"]
        if (
            stats["avg_return"] is None
            or stats["win_rate"] is None
            or stats["avg_return"] <= thresholds["min_avg_return"]
            or stats["win_rate"] < thresholds["min_win_rate"]
            or profit_factor is None
            or profit_factor < thresholds["min_profit_factor"]
        ):
            weak_segments.append(
                {
                    "name": name,
                    "sample_count": stats["trade_count"],
                    "win_rate": stats["win_rate"],
                    "avg_return": stats["avg_return"],
                    "profit_factor": profit_factor,
                }
            )
    return {"segment_count": eligible_count, "weak_segments": weak_segments[:8]}


def _candidate_reasons(
    stats: dict[str, Any],
    segment_report: dict[str, Any],
    *,
    thresholds: dict[str, Any],
) -> list[str]:
    reasons = []
    if stats["trade_count"] < thresholds["min_samples"]:
        reasons.append("sample_count_below_minimum")
    if stats["avg_return"] is None or stats["avg_return"] <= thresholds["min_avg_return"]:
        reasons.append("avg_return_below_minimum")
    if stats["win_rate"] is None or stats["win_rate"] < thresholds["min_win_rate"]:
        reasons.append("win_rate_below_minimum")
    if stats["profit_factor"] is None or stats["profit_factor"] < thresholds["min_profit_factor"]:
        reasons.append("profit_factor_below_minimum")
    if segment_report["segment_count"] < thresholds["min_segments"]:
        reasons.append("segment_count_below_minimum")
    if segment_report["weak_segments"]:
        reasons.append("weak_segments")
    return reasons


def _segment_candidate_reasons(
    stats: dict[str, Any],
    *,
    quality: dict[str, Any] | None = None,
    thresholds: dict[str, Any],
) -> list[str]:
    reasons = []
    if stats["trade_count"] < thresholds["min_samples"]:
        reasons.append("sample_count_below_minimum")
    if quality:
        if quality["unique_time_bucket_count"] < thresholds["min_unique_time_buckets"]:
            reasons.append("time_bucket_count_below_minimum")
        if quality["unique_event_day_count"] < thresholds["min_unique_event_days"]:
            reasons.append("event_day_count_below_minimum")
        if quality["unique_market_window_count"] < thresholds["min_unique_market_windows"]:
            reasons.append("market_window_count_below_minimum")
        if quality["unique_collection_run_count"] < thresholds["min_unique_collection_runs"]:
            reasons.append("collection_run_count_below_minimum")
        if quality["max_same_return_count"] > thresholds["max_same_return_samples"]:
            reasons.append("same_return_cluster_too_large")
        if quality["return_cluster_ratio"] > thresholds["max_return_cluster_ratio"]:
            reasons.append("return_cluster_ratio_too_high")
    if stats["avg_return"] is None or stats["avg_return"] <= thresholds["min_avg_return"]:
        reasons.append("avg_return_below_minimum")
    if stats["win_rate"] is None or stats["win_rate"] < thresholds["min_win_rate"]:
        reasons.append("win_rate_below_minimum")
    if stats["profit_factor"] is None or stats["profit_factor"] < thresholds["min_profit_factor"]:
        reasons.append("profit_factor_below_minimum")
    return reasons


def _promotion_status(reasons: list[str]) -> str:
    if not reasons:
        return "candidate"
    if set(reasons).issubset({"segment_count_below_minimum", "weak_segments"}):
        return "watchlist"
    return "rejected"


def _pseudo_trade_stats(items: list[FeaturePair]) -> dict[str, Any]:
    values = [float(label.return_pct) for _event, label, _snapshot, _run in items if isinstance(label.return_pct, (int, float))]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trade_count": len(values),
        "win_rate": len(wins) / len(values) if values else None,
        "avg_return": mean(values) if values else None,
        "median_return": _median(values),
        "profit_factor": gross_win / gross_loss if gross_loss else (999.0 if gross_win else None),
        "avg_mfe": _avg_label(items, "mfe"),
        "avg_mae": _avg_label(items, "mae"),
        "gross_win": gross_win,
        "gross_loss": gross_loss,
    }


def _arm_edge(candidate: dict[str, Any], control: dict[str, Any]) -> dict[str, Any]:
    return {
        "avg_return_delta": _delta(candidate.get("avg_return"), control.get("avg_return")),
        "win_rate_delta": _delta(candidate.get("win_rate"), control.get("win_rate")),
        "profit_factor_delta": _delta(candidate.get("profit_factor"), control.get("profit_factor")),
        "candidate_trade_ratio": (
            candidate["trade_count"] / (candidate["trade_count"] + control["trade_count"])
            if candidate["trade_count"] + control["trade_count"]
            else 0.0
        ),
    }


async def _persist_experiment(
    session: AsyncSession,
    *,
    name: str,
    scope: dict[str, Any],
    params: dict[str, Any],
    metrics: dict[str, Any],
    notes: str,
) -> dict[str, Any]:
    stored_metrics = {key: value for key, value in metrics.items() if key != "experiment_run"}
    item = ExperimentRun(
        name=name,
        status="research",
        scope=scope,
        params=params,
        metrics=stored_metrics,
        notes=notes,
    )
    session.add(item)
    await session.commit()
    return {"id": item.id, "name": item.name, "status": item.status}


def _thresholds(
    *,
    min_samples: int,
    min_win_rate: float,
    min_profit_factor: float,
    min_avg_return: float,
    segment_min_samples: int,
    min_segments: int,
    dedupe_research_samples: bool = DEFAULT_DEDUPE_RESEARCH_SAMPLES,
    dedupe_bucket_minutes: int = DEFAULT_DEDUPE_BUCKET_MINUTES,
    min_unique_time_buckets: int = DEFAULT_MIN_UNIQUE_TIME_BUCKETS,
    min_unique_event_days: int = DEFAULT_MIN_UNIQUE_EVENT_DAYS,
    min_unique_market_windows: int = DEFAULT_MIN_UNIQUE_MARKET_WINDOWS,
    min_unique_collection_runs: int = DEFAULT_MIN_UNIQUE_COLLECTION_RUNS,
    market_window_hours: int = DEFAULT_MARKET_WINDOW_HOURS,
    max_same_return_samples: int = DEFAULT_MAX_SAME_RETURN_SAMPLES,
    max_return_cluster_ratio: float = DEFAULT_MAX_RETURN_CLUSTER_RATIO,
) -> dict[str, Any]:
    return {
        "min_samples": int(min_samples),
        "min_win_rate": float(min_win_rate),
        "min_profit_factor": float(min_profit_factor),
        "min_avg_return": float(min_avg_return),
        "segment_min_samples": int(segment_min_samples),
        "min_segments": int(min_segments),
        "dedupe_research_samples": bool(dedupe_research_samples),
        "dedupe_bucket_minutes": max(1, int(dedupe_bucket_minutes)),
        "min_unique_time_buckets": max(1, int(min_unique_time_buckets)),
        "min_unique_event_days": max(1, int(min_unique_event_days)),
        "min_unique_market_windows": max(1, int(min_unique_market_windows)),
        "min_unique_collection_runs": max(1, int(min_unique_collection_runs)),
        "market_window_hours": max(1, min(24, int(market_window_hours))),
        "max_same_return_samples": max(1, int(max_same_return_samples)),
        "max_return_cluster_ratio": float(max_return_cluster_ratio),
    }


def _research_policy() -> dict[str, Any]:
    return {
        "research_only": True,
        "opens_paper_trades": False,
        "changes_strategy_weights": False,
        "used_for_live_trading": False,
        "used_for_execution_weights": False,
        "used_for_opening_decisions": False,
        "reason": "Candidate screening and paper A/B are research reports until separately promoted.",
    }


def _rejection_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        for reason in row["rejection_reasons"]:
            counter[reason] += 1
    return dict(sorted(counter.items()))


def _dedupe_research_pairs(
    items: list[FeaturePair],
    *,
    thresholds: dict[str, Any],
    key_func: Callable[[FeatureEventModel], str],
) -> list[FeaturePair]:
    if not thresholds.get("dedupe_research_samples", DEFAULT_DEDUPE_RESEARCH_SAMPLES):
        return items
    bucket_minutes = max(1, int(thresholds.get("dedupe_bucket_minutes") or DEFAULT_DEDUPE_BUCKET_MINUTES))
    seen: set[tuple[str, str]] = set()
    deduped: list[FeaturePair] = []
    for item in items:
        event = item[0]
        key = (key_func(event), _time_bucket_key(event.event_ts, bucket_minutes))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _sample_quality(
    raw_items: list[FeaturePair],
    effective_items: list[FeaturePair],
    *,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    bucket_minutes = max(1, int(thresholds.get("dedupe_bucket_minutes") or DEFAULT_DEDUPE_BUCKET_MINUTES))
    market_window_hours = max(1, int(thresholds.get("market_window_hours") or DEFAULT_MARKET_WINDOW_HOURS))
    raw_count = len(raw_items)
    effective_count = len(effective_items)
    return_counts = Counter(_return_cluster_key(label.return_pct) for _event, label, _snapshot, _run in effective_items)
    max_same_return_count = max(return_counts.values(), default=0)
    return_cluster_ratio = max_same_return_count / effective_count if effective_count else 0.0
    unique_event_ts = {_event_ts_key(event.event_ts) for event, _label, _snapshot, _run in raw_items}
    unique_event_days = {_day_key(event.event_ts) for event, _label, _snapshot, _run in raw_items}
    unique_market_windows = {
        _market_window_key(event.event_ts, market_window_hours) for event, _label, _snapshot, _run in raw_items
    }
    unique_snapshots = {str(event.snapshot_id) for event, _label, _snapshot, _run in raw_items if event.snapshot_id}
    unique_time_buckets = {_time_bucket_key(event.event_ts, bucket_minutes) for event, _label, _snapshot, _run in raw_items}
    unique_collection_runs = {
        _collection_run_key(event, snapshot, run, bucket_minutes)
        for event, _label, snapshot, run in raw_items
    }
    risk_reasons: list[str] = []
    if len(unique_time_buckets) < int(thresholds["min_unique_time_buckets"]):
        risk_reasons.append("time_bucket_count_below_minimum")
    if len(unique_event_days) < int(thresholds["min_unique_event_days"]):
        risk_reasons.append("event_day_count_below_minimum")
    if len(unique_market_windows) < int(thresholds["min_unique_market_windows"]):
        risk_reasons.append("market_window_count_below_minimum")
    if len(unique_collection_runs) < int(thresholds["min_unique_collection_runs"]):
        risk_reasons.append("collection_run_count_below_minimum")
    if max_same_return_count > int(thresholds["max_same_return_samples"]):
        risk_reasons.append("same_return_cluster_too_large")
    if return_cluster_ratio > float(thresholds["max_return_cluster_ratio"]):
        risk_reasons.append("return_cluster_ratio_too_high")
    dedupe_removed_count = raw_count - effective_count
    dedupe_ratio = dedupe_removed_count / raw_count if raw_count else 0.0
    overfit_risk = "low"
    if risk_reasons:
        overfit_risk = "high"
    elif dedupe_ratio >= 0.5 or (effective_count and len(unique_snapshots) < effective_count):
        overfit_risk = "medium"
    return {
        "dedupe_research_samples": bool(thresholds.get("dedupe_research_samples")),
        "dedupe_bucket_minutes": bucket_minutes,
        "market_window_hours": market_window_hours,
        "raw_sample_count": raw_count,
        "effective_sample_count": effective_count,
        "dedupe_removed_count": dedupe_removed_count,
        "dedupe_ratio": dedupe_ratio,
        "unique_event_ts_count": len(unique_event_ts),
        "unique_event_day_count": len(unique_event_days),
        "unique_market_window_count": len(unique_market_windows),
        "unique_collection_run_count": len(unique_collection_runs),
        "unique_snapshot_count": len(unique_snapshots),
        "unique_time_bucket_count": len(unique_time_buckets),
        "max_same_return_count": max_same_return_count,
        "return_cluster_ratio": return_cluster_ratio,
        "overfit_risk": overfit_risk,
        "risk_reasons": risk_reasons,
    }


def _research_quality_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    qualities = [row.get("quality") or {} for row in rows]
    raw_count = sum(int(item.get("raw_sample_count") or 0) for item in qualities)
    effective_count = sum(int(item.get("effective_sample_count") or 0) for item in qualities)
    dedupe_removed_count = sum(int(item.get("dedupe_removed_count") or 0) for item in qualities)
    max_event_days = max((int(item.get("unique_event_day_count") or 0) for item in qualities), default=0)
    max_market_windows = max((int(item.get("unique_market_window_count") or 0) for item in qualities), default=0)
    max_collection_runs = max((int(item.get("unique_collection_run_count") or 0) for item in qualities), default=0)
    return {
        "raw_sample_count": raw_count,
        "effective_sample_count": effective_count,
        "dedupe_removed_count": dedupe_removed_count,
        "dedupe_ratio": dedupe_removed_count / raw_count if raw_count else 0.0,
        "max_unique_event_day_count": max_event_days,
        "max_unique_market_window_count": max_market_windows,
        "max_unique_collection_run_count": max_collection_runs,
        "risk_counts": _risk_summary(rows),
    }


def _risk_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        risk = str(row.get("overfit_risk") or (row.get("quality") or {}).get("overfit_risk") or "unknown")
        counter[risk] += 1
    return dict(sorted(counter.items()))


def _combined_overfit_risk(rows: list[dict[str, Any]]) -> str:
    risks = {str(row.get("overfit_risk") or "unknown") for row in rows}
    if "high" in risks:
        return "high"
    if "medium" in risks:
        return "medium"
    if "low" in risks:
        return "low"
    return "unknown"


def _segment_candidate_feature_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row["feature_key"]), []).append(row)
    summary = []
    for feature_key, items in buckets.items():
        total_samples = sum(int(row["sample_count"] or 0) for row in items)
        total_raw_samples = sum(int(row.get("raw_sample_count") or row["sample_count"] or 0) for row in items)
        weighted_return = _weighted_mean(items, "avg_return", "sample_count")
        weighted_win_rate = _weighted_mean(items, "win_rate", "sample_count")
        first = items[0]
        summary.append(
            {
                "feature_key": feature_key,
                "indicator": first["indicator"],
                "feature_name": first["feature_name"],
                "subtype": first["subtype"],
                "direction": first["direction"],
                "segment_count": len(items),
                "sample_count": total_samples,
                "raw_sample_count": total_raw_samples,
                "weighted_avg_return": weighted_return,
                "weighted_win_rate": weighted_win_rate,
                "overfit_risk": _combined_overfit_risk(items),
                "segments": [row["symbol_timeframe"] for row in items[:12]],
                "used_for_execution_weights": False,
                "used_for_opening_decisions": False,
            }
        )
    return sorted(
        summary,
        key=lambda row: (row["weighted_avg_return"] or -999.0, row["weighted_win_rate"] or 0.0, row["sample_count"]),
        reverse=True,
    )


def _feature_key(event: FeatureEventModel) -> str:
    return f"{event.feature_name}:{event.subtype}:{event.direction}"


def _symbol_timeframe(event: FeatureEventModel) -> str:
    return f"{event.symbol}:{event.timeframe}"


def _segment_key(event: FeatureEventModel) -> str:
    return f"{_feature_key(event)}:{event.symbol}:{event.timeframe}"


def _time_bucket_key(value: datetime, bucket_minutes: int) -> str:
    aware = _aware(value)
    bucket_seconds = max(1, int(bucket_minutes)) * 60
    bucket_start = int(aware.timestamp()) // bucket_seconds * bucket_seconds
    return datetime.fromtimestamp(bucket_start, tz=timezone.utc).isoformat()


def _event_ts_key(value: datetime) -> str:
    return _aware(value).isoformat()


def _day_key(value: datetime) -> str:
    return _aware(value).date().isoformat()


def _market_window_key(value: datetime, window_hours: int) -> str:
    aware = _aware(value)
    window = max(1, int(window_hours))
    return f"{aware.date().isoformat()}T{(aware.hour // window) * window:02d}"


def _collection_run_key(
    event: FeatureEventModel,
    snapshot: SignalSnapshot | None,
    collection_run: CollectionRun | None,
    bucket_minutes: int,
) -> str:
    if snapshot is not None and snapshot.collection_run_id:
        return f"run:{snapshot.collection_run_id}"
    if collection_run is not None and collection_run.id:
        return f"run:{collection_run.id}"
    if snapshot is not None:
        return f"snapshot_bucket:{_time_bucket_key(snapshot.collected_at, bucket_minutes)}"
    return f"event_bucket:{_time_bucket_key(event.event_ts, bucket_minutes)}"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _return_cluster_key(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.8f}"
    return "none"


def _avg_strength(items: list[FeaturePair]) -> float | None:
    values = [
        float(event.strength)
        for event, _label, _snapshot, _run in items
        if isinstance(event.strength, (int, float))
    ]
    return mean(values) if values else None


def _avg_label(items: list[FeaturePair], field: str) -> float | None:
    values = [getattr(label, field) for _event, label, _snapshot, _run in items]
    numeric = [float(value) for value in values if isinstance(value, (int, float))]
    return mean(numeric) if numeric else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _delta(left: Any, right: Any) -> float | None:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) - float(right)
    return None


def _weighted_mean(rows: list[dict[str, Any]], value_key: str, weight_key: str) -> float | None:
    total_weight = 0
    total = 0.0
    for row in rows:
        value = row.get(value_key)
        weight = int(row.get(weight_key) or 0)
        if not isinstance(value, (int, float)) or weight <= 0:
            continue
        total += float(value) * weight
        total_weight += weight
    return total / total_weight if total_weight else None


def _validate_horizon(horizon: str) -> None:
    if horizon not in FEATURE_HORIZONS:
        raise ValueError(f"Unsupported feature label horizon: {horizon}")
