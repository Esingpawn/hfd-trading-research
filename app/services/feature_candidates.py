from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import sqrt
from statistics import mean
from typing import Any, Callable

from sqlalchemy import func, select, text
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
DEFAULT_MIN_PROFIT_FACTOR_LOWER = 1.0
DEFAULT_TIME_SPLIT_MIN_SAMPLES = 30
DEFAULT_RESEARCH_SAMPLE_FETCH_MULTIPLIER = 10
DEFAULT_RESEARCH_SAMPLE_MAX_FETCH_ROWS = 50000
DEFAULT_SEGMENT_COVERAGE_TARGET = 30
DEFAULT_RESEARCH_QUERY_MAX_LIMIT = 5000
DEFAULT_RESEARCH_REPORT_MAX_LIMIT = DEFAULT_RESEARCH_QUERY_MAX_LIMIT
DEFAULT_RESEARCH_REPORT_STATEMENT_TIMEOUT_MS = 45000
DEFAULT_RESEARCH_REPORT_MAX_AGE_SECONDS = 3600
RESEARCH_REPORT_ADVISORY_LOCK_ID = 78234901
CONFIDENCE_Z = 1.96
DEFAULT_RESEARCH_REPORT_NAMES = (
    "feature_candidates",
    "feature_paper_ab",
    "feature_segment_candidates",
    "feature_segment_paper_ab",
)


@dataclass(frozen=True)
class SnapshotRef:
    id: str | None
    collection_run_id: str | None
    collected_at: datetime


@dataclass(frozen=True)
class CollectionRunRef:
    id: str | None
    started_at: datetime
    finished_at: datetime | None


FeaturePair = tuple[FeatureEventModel, FeatureLabel, SnapshotRef | None, CollectionRunRef | None]


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
    requested_limit = int(limit)
    limit = _bounded_research_limit(requested_limit)
    thresholds = _thresholds(
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_avg_return=min_avg_return,
        segment_min_samples=segment_min_samples,
        min_segments=min_segments,
    )
    pairs = await _labeled_feature_pairs(session, horizon=horizon, limit=limit, coverage_target=min_samples)
    return _feature_candidate_screen_from_pairs(
        pairs,
        horizon=horizon,
        limit=limit,
        requested_limit=requested_limit,
        thresholds=thresholds,
        persist=False,
        session=session,
    ) if not persist else await _feature_candidate_screen_from_pairs_persisted(
        session,
        pairs,
        horizon=horizon,
        limit=limit,
        requested_limit=requested_limit,
        thresholds=thresholds,
    )


def _feature_candidate_screen_from_pairs(
    pairs: list[FeaturePair],
    *,
    horizon: str,
    limit: int,
    requested_limit: int | None = None,
    thresholds: dict[str, Any],
    persist: bool = False,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    rows = _candidate_rows(pairs, thresholds=thresholds)
    candidates = [row for row in rows if row["paper_ab_ready"]]
    watchlist = [row for row in rows if row["promotion_status"] == "watchlist"]
    report: dict[str, Any] = {
        "horizon": horizon,
        "limit": limit,
        "requested_limit": requested_limit if requested_limit is not None else limit,
        "limit_capped": requested_limit is not None and requested_limit != limit,
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
        raise ValueError("Use _feature_candidate_screen_from_pairs_persisted for persisted reports")
    return report


async def _feature_candidate_screen_from_pairs_persisted(
    session: AsyncSession,
    pairs: list[FeaturePair],
    *,
    horizon: str,
    limit: int,
    requested_limit: int | None = None,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    report = _feature_candidate_screen_from_pairs(
        pairs,
        horizon=horizon,
        limit=limit,
        requested_limit=requested_limit,
        thresholds=thresholds,
    )
    report["experiment_run"] = await _persist_experiment(
        session,
        name=f"feature_candidates_{horizon}",
        scope={
            "horizon": horizon,
            "requested_limit": requested_limit if requested_limit is not None else limit,
            "limit": limit,
            "labeled_count": len(pairs),
        },
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
    requested_limit = int(limit)
    limit = _bounded_research_limit(requested_limit)
    thresholds = _thresholds(
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_avg_return=min_avg_return,
        segment_min_samples=segment_min_samples,
        min_segments=min_segments,
    )
    pairs = await _labeled_feature_pairs(session, horizon=horizon, limit=limit, coverage_target=min_samples)
    report = _feature_paper_ab_from_pairs(
        pairs,
        horizon=horizon,
        candidate_limit=candidate_limit,
        limit=limit,
        requested_limit=requested_limit,
        thresholds=thresholds,
    )
    if persist:
        report["experiment_run"] = await _persist_experiment(
            session,
            name=f"feature_paper_ab_{horizon}",
            scope={
                "horizon": horizon,
                "requested_limit": requested_limit,
                "limit": limit,
                "candidate_limit": candidate_limit,
            },
            params=thresholds,
            metrics=report,
            notes="Report-only paper A/B using feature labels as pseudo-trades; no PaperTrade rows are opened.",
        )
    return report


def _feature_paper_ab_from_pairs(
    pairs: list[FeaturePair],
    *,
    horizon: str,
    candidate_limit: int,
    limit: int,
    requested_limit: int | None = None,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    candidate_report = _feature_candidate_screen_from_pairs(
        pairs,
        horizon=horizon,
        limit=limit,
        requested_limit=requested_limit,
        thresholds=thresholds,
    )
    selected = candidate_report["candidates"][:candidate_limit]
    selected_keys = {str(row["feature_key"]) for row in selected}
    candidate_pairs = [item for item in pairs if _feature_key(item[0]) in selected_keys]
    control_pairs = [item for item in pairs if _feature_key(item[0]) not in selected_keys]
    candidate_stats = _pseudo_trade_stats(candidate_pairs)
    control_stats = _pseudo_trade_stats(control_pairs)
    report: dict[str, Any] = {
        "horizon": horizon,
        "limit": limit,
        "requested_limit": requested_limit if requested_limit is not None else limit,
        "limit_capped": requested_limit is not None and requested_limit != limit,
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
    return report


async def _feature_paper_ab_from_pairs_persisted(
    session: AsyncSession,
    pairs: list[FeaturePair],
    *,
    horizon: str,
    candidate_limit: int,
    limit: int,
    requested_limit: int | None = None,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    report = _feature_paper_ab_from_pairs(
        pairs,
        horizon=horizon,
        candidate_limit=candidate_limit,
        limit=limit,
        requested_limit=requested_limit,
        thresholds=thresholds,
    )
    report["experiment_run"] = await _persist_experiment(
        session,
        name=f"feature_paper_ab_{horizon}",
        scope={
            "horizon": horizon,
            "requested_limit": requested_limit if requested_limit is not None else limit,
            "limit": limit,
            "candidate_limit": candidate_limit,
        },
        params=thresholds,
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
    requested_limit = int(limit)
    limit = _bounded_research_limit(requested_limit)
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
    pairs = await _labeled_feature_pairs(session, horizon=horizon, limit=limit, coverage_target=min_samples)
    return _feature_segment_candidate_screen_from_pairs(
        pairs,
        horizon=horizon,
        limit=limit,
        requested_limit=requested_limit,
        thresholds=thresholds,
    ) if not persist else await _feature_segment_candidate_screen_from_pairs_persisted(
        session,
        pairs,
        horizon=horizon,
        limit=limit,
        requested_limit=requested_limit,
        thresholds=thresholds,
    )


def _feature_segment_candidate_screen_from_pairs(
    pairs: list[FeaturePair],
    *,
    horizon: str,
    limit: int,
    requested_limit: int | None = None,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    rows = _segment_candidate_rows(pairs, thresholds=thresholds)
    candidates = [row for row in rows if row["paper_ab_ready"]]
    return {
        "horizon": horizon,
        "limit": limit,
        "requested_limit": requested_limit if requested_limit is not None else limit,
        "limit_capped": requested_limit is not None and requested_limit != limit,
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


async def _feature_segment_candidate_screen_from_pairs_persisted(
    session: AsyncSession,
    pairs: list[FeaturePair],
    *,
    horizon: str,
    limit: int,
    requested_limit: int | None = None,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    report = _feature_segment_candidate_screen_from_pairs(
        pairs,
        horizon=horizon,
        limit=limit,
        requested_limit=requested_limit,
        thresholds=thresholds,
    )
    report["experiment_run"] = await _persist_experiment(
        session,
        name=f"feature_segment_candidates_{horizon}",
        scope={
            "horizon": horizon,
            "requested_limit": requested_limit if requested_limit is not None else limit,
            "limit": limit,
            "labeled_count": len(pairs),
        },
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
    requested_limit = int(limit)
    limit = _bounded_research_limit(requested_limit)
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
    pairs = await _labeled_feature_pairs(session, horizon=horizon, limit=limit, coverage_target=min_samples)
    report = _feature_segment_paper_ab_from_pairs(
        pairs,
        horizon=horizon,
        candidate_limit=candidate_limit,
        limit=limit,
        requested_limit=requested_limit,
        thresholds=thresholds,
    )
    if persist:
        report["experiment_run"] = await _persist_experiment(
            session,
            name=f"feature_segment_paper_ab_{horizon}",
            scope={
                "horizon": horizon,
                "requested_limit": requested_limit,
                "limit": limit,
                "candidate_limit": candidate_limit,
            },
            params=thresholds,
            metrics=report,
            notes="Report-only segment-aware paper A/B using feature labels as pseudo-trades; no PaperTrade rows are opened.",
        )
    return report


def _feature_segment_paper_ab_from_pairs(
    pairs: list[FeaturePair],
    *,
    horizon: str,
    candidate_limit: int,
    limit: int,
    requested_limit: int | None = None,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    segment_report = _feature_segment_candidate_screen_from_pairs(
        pairs,
        horizon=horizon,
        limit=limit,
        requested_limit=requested_limit,
        thresholds=thresholds,
    )
    selected = segment_report["candidates"][:candidate_limit]
    selected_keys = {str(row["segment_key"]) for row in selected}
    selected_symbol_timeframes = {str(row["symbol_timeframe"]) for row in selected}
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
        "requested_limit": requested_limit if requested_limit is not None else limit,
        "limit_capped": requested_limit is not None and requested_limit != limit,
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
    return report


async def _feature_segment_paper_ab_from_pairs_persisted(
    session: AsyncSession,
    pairs: list[FeaturePair],
    *,
    horizon: str,
    candidate_limit: int,
    limit: int,
    requested_limit: int | None = None,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    report = _feature_segment_paper_ab_from_pairs(
        pairs,
        horizon=horizon,
        candidate_limit=candidate_limit,
        limit=limit,
        requested_limit=requested_limit,
        thresholds=thresholds,
    )
    report["experiment_run"] = await _persist_experiment(
        session,
        name=f"feature_segment_paper_ab_{horizon}",
        scope={
            "horizon": horizon,
            "requested_limit": requested_limit if requested_limit is not None else limit,
            "limit": limit,
            "candidate_limit": candidate_limit,
        },
        params=thresholds,
        metrics=report,
        notes="Report-only segment-aware paper A/B using feature labels as pseudo-trades; no PaperTrade rows are opened.",
    )
    return report


async def latest_feature_candidate_screen(
    session: AsyncSession,
    *,
    horizon: str = "30m",
) -> dict[str, Any]:
    return await _latest_persisted_report(
        session,
        name=f"feature_candidates_{horizon}",
        empty_factory=lambda: _empty_feature_candidate_report(horizon=horizon),
    )


async def latest_feature_paper_ab(
    session: AsyncSession,
    *,
    horizon: str = "30m",
) -> dict[str, Any]:
    return await _latest_persisted_report(
        session,
        name=f"feature_paper_ab_{horizon}",
        empty_factory=lambda: _empty_feature_paper_ab_report(horizon=horizon),
    )


async def latest_feature_segment_candidate_screen(
    session: AsyncSession,
    *,
    horizon: str = "30m",
) -> dict[str, Any]:
    return await _latest_persisted_report(
        session,
        name=f"feature_segment_candidates_{horizon}",
        empty_factory=lambda: _empty_feature_segment_candidate_report(horizon=horizon),
    )


async def latest_feature_segment_paper_ab(
    session: AsyncSession,
    *,
    horizon: str = "30m",
) -> dict[str, Any]:
    return await _latest_persisted_report(
        session,
        name=f"feature_segment_paper_ab_{horizon}",
        empty_factory=lambda: _empty_feature_segment_paper_ab_report(horizon=horizon),
    )


async def generate_default_research_reports(
    session: AsyncSession,
    *,
    horizon: str = "30m",
    min_samples: int = DEFAULT_MIN_SAMPLES,
    limit: int = 5000,
    max_age_seconds: int | None = None,
) -> dict[str, Any]:
    requested_limit = int(limit)
    limit = _bounded_research_limit(requested_limit)
    reports: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    _validate_horizon(horizon)
    freshness = await research_report_freshness(session, horizon=horizon, max_age_seconds=max_age_seconds)
    if freshness["fresh"]:
        return {
            "enabled": True,
            "status": "skipped",
            "skip_reason": "research_reports_fresh",
            "horizon": horizon,
            "min_samples": min_samples,
            "requested_limit": requested_limit,
            "limit": limit,
            "generated_count": 0,
            "error_count": 0,
            "errors": [],
            "freshness": freshness,
            "reports": {},
        }
    lock_acquired = await _try_research_report_lock(session)
    if not lock_acquired:
        return {
            "enabled": True,
            "status": "skipped",
            "skip_reason": "research_report_already_running",
            "horizon": horizon,
            "min_samples": min_samples,
            "requested_limit": requested_limit,
            "limit": limit,
            "generated_count": 0,
            "error_count": 0,
            "errors": [],
            "freshness": freshness,
            "reports": {},
        }
    await _set_research_statement_timeout(session)
    pairs = await _labeled_feature_pairs(session, horizon=horizon, limit=limit, coverage_target=min_samples)
    feature_thresholds = _thresholds(
        min_samples=min_samples,
        min_win_rate=DEFAULT_MIN_WIN_RATE,
        min_profit_factor=DEFAULT_MIN_PROFIT_FACTOR,
        min_avg_return=DEFAULT_MIN_AVG_RETURN,
        segment_min_samples=DEFAULT_SEGMENT_MIN_SAMPLES,
        min_segments=DEFAULT_MIN_SEGMENTS,
    )
    segment_thresholds = _thresholds(
        min_samples=min_samples,
        min_win_rate=DEFAULT_MIN_WIN_RATE,
        min_profit_factor=DEFAULT_MIN_PROFIT_FACTOR,
        min_avg_return=DEFAULT_MIN_AVG_RETURN,
        segment_min_samples=min_samples,
        min_segments=1,
    )

    async def run(name: str, func: Callable[[], Any]) -> None:
        try:
            reports[name] = await func()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            errors.append({"report": name, "error": str(exc)})

    await run(
        "feature_candidates",
        lambda: _feature_candidate_screen_from_pairs_persisted(
            session,
            pairs,
            horizon=horizon,
            limit=limit,
            requested_limit=requested_limit,
            thresholds=feature_thresholds,
        ),
    )
    await run(
        "feature_paper_ab",
        lambda: _feature_paper_ab_from_pairs_persisted(
            session,
            pairs,
            horizon=horizon,
            candidate_limit=20,
            limit=limit,
            requested_limit=requested_limit,
            thresholds=feature_thresholds,
        ),
    )
    await run(
        "feature_segment_candidates",
        lambda: _feature_segment_candidate_screen_from_pairs_persisted(
            session,
            pairs,
            horizon=horizon,
            limit=limit,
            requested_limit=requested_limit,
            thresholds=segment_thresholds,
        ),
    )
    await run(
        "feature_segment_paper_ab",
        lambda: _feature_segment_paper_ab_from_pairs_persisted(
            session,
            pairs,
            horizon=horizon,
            candidate_limit=50,
            limit=limit,
            requested_limit=requested_limit,
            thresholds=segment_thresholds,
        ),
    )
    return {
        "enabled": True,
        "horizon": horizon,
        "min_samples": min_samples,
        "requested_limit": requested_limit,
        "limit": limit,
        "labeled_count": len(pairs),
        "generated_count": len(reports),
        "error_count": len(errors),
        "errors": errors,
        "freshness": await research_report_freshness(session, horizon=horizon, max_age_seconds=max_age_seconds),
        "reports": {name: _report_summary(report) for name, report in reports.items()},
    }


async def research_report_freshness(
    session: AsyncSession,
    *,
    horizon: str = "30m",
    max_age_seconds: int | None = None,
) -> dict[str, Any]:
    _validate_horizon(horizon)
    max_age = DEFAULT_RESEARCH_REPORT_MAX_AGE_SECONDS if max_age_seconds is None else int(max_age_seconds)
    now = datetime.now(timezone.utc)
    names = [f"{name}_{horizon}" for name in DEFAULT_RESEARCH_REPORT_NAMES]
    rows = await session.execute(
        select(ExperimentRun.name, func.max(ExperimentRun.created_at))
        .where(ExperimentRun.name.in_(names), ExperimentRun.status == "research")
        .group_by(ExperimentRun.name)
    )
    generated_at = {str(name): created_at for name, created_at in rows.all() if created_at is not None}
    reports: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    stale: list[str] = []
    oldest_age = 0.0
    for name in names:
        short_name = name.removesuffix(f"_{horizon}")
        created_at = generated_at.get(name)
        if created_at is None:
            missing.append(short_name)
            reports[short_name] = {"generated_at": None, "age_seconds": None, "fresh": False}
            continue
        age = max(0.0, (now - _aware(created_at)).total_seconds())
        oldest_age = max(oldest_age, age)
        is_fresh = age <= max_age
        if not is_fresh:
            stale.append(short_name)
        reports[short_name] = {
            "generated_at": created_at,
            "age_seconds": age,
            "fresh": is_fresh,
        }
    return {
        "horizon": horizon,
        "max_age_seconds": max_age,
        "fresh": not missing and not stale,
        "missing": missing,
        "stale": stale,
        "oldest_age_seconds": oldest_age if generated_at else None,
        "reports": reports,
    }


async def _try_research_report_lock(session: AsyncSession) -> bool:
    if session.get_bind().dialect.name != "postgresql":
        return True
    value = await session.scalar(text("select pg_try_advisory_lock(:lock_id)"), {"lock_id": RESEARCH_REPORT_ADVISORY_LOCK_ID})
    return bool(value)


def _bounded_research_limit(limit: int) -> int:
    return min(max(1, int(limit)), DEFAULT_RESEARCH_QUERY_MAX_LIMIT)


async def _set_research_statement_timeout(session: AsyncSession) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(
        text(f"set statement_timeout = {int(DEFAULT_RESEARCH_REPORT_STATEMENT_TIMEOUT_MS)}"),
    )


async def _labeled_feature_pairs(
    session: AsyncSession,
    *,
    horizon: str,
    limit: int,
    coverage_target: int = DEFAULT_SEGMENT_COVERAGE_TARGET,
) -> list[FeaturePair]:
    fetch_limit = _research_fetch_limit(limit)
    row_items = await _balanced_labeled_feature_rows(
        session,
        horizon=horizon,
        limit=fetch_limit,
        coverage_target=coverage_target,
    )
    collection_runs = await _collection_run_windows(session)
    pairs = [
        (event, label, snapshot, collection_run or _infer_collection_run(snapshot, collection_runs))
        for event, label, snapshot, collection_run in row_items
        if isinstance(label.return_pct, (int, float)) and event.direction in {"long", "short"}
    ]
    return _coverage_sample_pairs(pairs, limit=limit, coverage_target=coverage_target)


def _research_fetch_limit(limit: int) -> int:
    if limit <= 0:
        return 0
    return max(limit, min(DEFAULT_RESEARCH_SAMPLE_MAX_FETCH_ROWS, limit * DEFAULT_RESEARCH_SAMPLE_FETCH_MULTIPLIER))


def _coverage_sample_pairs(
    pairs: list[FeaturePair],
    *,
    limit: int,
    coverage_target: int = DEFAULT_SEGMENT_COVERAGE_TARGET,
) -> list[FeaturePair]:
    if limit <= 0:
        return []
    if len(pairs) <= limit:
        return sorted(pairs, key=lambda row: _aware(row[0].event_ts), reverse=True)
    selected: list[FeaturePair] = []
    selected_ids: set[str] = set()
    segment_counts: Counter[str] = Counter()
    segment_bucket_counts: Counter[tuple[str, str]] = Counter()
    segment_days: dict[str, set[str]] = {}
    segment_runs: dict[str, set[str]] = {}
    ordered = sorted(pairs, key=lambda row: _coverage_sample_priority(row), reverse=True)
    segment_items: dict[str, list[FeaturePair]] = {}
    available_days: dict[str, set[str]] = {}
    available_runs: dict[str, set[str]] = {}
    for item in ordered:
        event, _label, snapshot, collection_run = item
        segment = _segment_key(event)
        segment_items.setdefault(segment, []).append(item)
        available_days.setdefault(segment, set()).add(_day_key(event.event_ts))
        available_runs.setdefault(segment, set()).add(_collection_run_key(event, snapshot, collection_run, DEFAULT_DEDUPE_BUCKET_MINUTES))
    target = max(1, min(int(coverage_target), limit))
    segments_by_depth = sorted(
        segment_items,
        key=lambda key: _coverage_segment_priority(
            segment_items[key],
            available_days=available_days.get(key) or set(),
            available_runs=available_runs.get(key) or set(),
            target=target,
        ),
        reverse=True,
    )
    for segment in _diverse_targetable_segments(
        segments_by_depth,
        segment_items=segment_items,
        available_days=available_days,
        available_runs=available_runs,
        target=target,
    ):
        _fill_coverage_segment(
            segment,
            segment_items=segment_items,
            selected=selected,
            selected_ids=selected_ids,
            segment_counts=segment_counts,
            segment_bucket_counts=segment_bucket_counts,
            segment_days=segment_days,
            segment_runs=segment_runs,
            available_days=available_days,
            available_runs=available_runs,
            desired_count=target,
            limit=limit,
        )
        if len(selected) >= limit:
            return sorted(selected, key=lambda row: _aware(row[0].event_ts), reverse=True)
    for segment in _diverse_underfilled_segments(
        segments_by_depth,
        segment_items=segment_items,
        available_days=available_days,
        available_runs=available_runs,
        target=target,
    ):
        _fill_coverage_segment(
            segment,
            segment_items=segment_items,
            selected=selected,
            selected_ids=selected_ids,
            segment_counts=segment_counts,
            segment_bucket_counts=segment_bucket_counts,
            segment_days=segment_days,
            segment_runs=segment_runs,
            available_days=available_days,
            available_runs=available_runs,
            desired_count=min(len(segment_items[segment]), target),
            limit=limit,
        )
        if len(selected) >= limit:
            return sorted(selected, key=lambda row: _aware(row[0].event_ts), reverse=True)
    for expansion_target in _coverage_targets(limit, coverage_target=target):
        made_progress = True
        while made_progress:
            made_progress = False
            for segment in segments_by_depth:
                before = len(selected)
                _fill_coverage_segment(
                    segment,
                    segment_items=segment_items,
                    selected=selected,
                    selected_ids=selected_ids,
                    segment_counts=segment_counts,
                    segment_bucket_counts=segment_bucket_counts,
                    segment_days=segment_days,
                    segment_runs=segment_runs,
                    available_days=available_days,
                    available_runs=available_runs,
                    desired_count=expansion_target,
                    limit=limit,
                )
                made_progress = made_progress or len(selected) > before
                if len(selected) >= limit:
                    return sorted(selected, key=lambda row: _aware(row[0].event_ts), reverse=True)
    for item in ordered:
        event = item[0]
        if event.id in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(event.id)
        if len(selected) >= limit:
            break
    return sorted(selected, key=lambda row: _aware(row[0].event_ts), reverse=True)


def _next_coverage_item(
    items: list[FeaturePair],
    *,
    selected_ids: set[str],
    selected_buckets: Counter[tuple[str, str]],
    selected_days: set[str],
    selected_runs: set[str],
    available_day_count: int,
    available_run_count: int,
    target: int,
) -> FeaturePair | None:
    fallback: FeaturePair | None = None
    segment = _segment_key(items[0][0]) if items else ""
    for item in items:
        event, _label, snapshot, collection_run = item
        if event.id in selected_ids:
            continue
        bucket = _time_bucket_key(event.event_ts, DEFAULT_DEDUPE_BUCKET_MINUTES)
        if selected_buckets[(segment, bucket)] >= 1:
            continue
        day = _day_key(event.event_ts)
        run_key = _collection_run_key(event, snapshot, collection_run, DEFAULT_DEDUPE_BUCKET_MINUTES)
        needs_new_day = day in selected_days and len(selected_days) < min(target, available_day_count)
        needs_new_run = run_key in selected_runs and len(selected_runs) < min(target, available_run_count)
        if needs_new_day or needs_new_run:
            if fallback is None:
                fallback = item
            continue
        return item
    return fallback


def _fill_coverage_segment(
    segment: str,
    *,
    segment_items: dict[str, list[FeaturePair]],
    selected: list[FeaturePair],
    selected_ids: set[str],
    segment_counts: Counter[str],
    segment_bucket_counts: Counter[tuple[str, str]],
    segment_days: dict[str, set[str]],
    segment_runs: dict[str, set[str]],
    available_days: dict[str, set[str]],
    available_runs: dict[str, set[str]],
    desired_count: int,
    limit: int,
) -> None:
    while len(selected) < limit and segment_counts[segment] < desired_count:
        item = _next_coverage_item(
            segment_items[segment],
            selected_ids=selected_ids,
            selected_buckets=segment_bucket_counts,
            selected_days=segment_days.setdefault(segment, set()),
            selected_runs=segment_runs.setdefault(segment, set()),
            available_day_count=len(available_days.get(segment) or ()),
            available_run_count=len(available_runs.get(segment) or ()),
            target=desired_count,
        )
        if item is None:
            return
        event, _label, snapshot, collection_run = item
        selected.append(item)
        selected_ids.add(event.id)
        segment_counts[segment] += 1
        segment_bucket_counts[(segment, _time_bucket_key(event.event_ts, DEFAULT_DEDUPE_BUCKET_MINUTES))] += 1
        segment_days[segment].add(_day_key(event.event_ts))
        segment_runs[segment].add(_collection_run_key(event, snapshot, collection_run, DEFAULT_DEDUPE_BUCKET_MINUTES))


def _diverse_targetable_segments(
    segments: list[str],
    *,
    segment_items: dict[str, list[FeaturePair]],
    available_days: dict[str, set[str]],
    available_runs: dict[str, set[str]],
    target: int,
) -> list[str]:
    return [
        segment
        for segment in segments
        if len(segment_items[segment]) >= target
        and _segment_has_diversity(
            segment_items[segment],
            available_days=available_days.get(segment) or set(),
            available_runs=available_runs.get(segment) or set(),
        )
    ]


def _diverse_underfilled_segments(
    segments: list[str],
    *,
    segment_items: dict[str, list[FeaturePair]],
    available_days: dict[str, set[str]],
    available_runs: dict[str, set[str]],
    target: int,
) -> list[str]:
    return [
        segment
        for segment in segments
        if len(segment_items[segment]) < target
        and _segment_has_diversity(
            segment_items[segment],
            available_days=available_days.get(segment) or set(),
            available_runs=available_runs.get(segment) or set(),
        )
    ]


def _segment_has_diversity(
    items: list[FeaturePair],
    *,
    available_days: set[str],
    available_runs: set[str],
) -> bool:
    windows = {_market_window_key(item[0].event_ts, DEFAULT_MARKET_WINDOW_HOURS) for item in items}
    return len(available_days) >= 2 and len(windows) >= 2 and len(available_runs) >= 2


def _coverage_targets(limit: int, *, coverage_target: int = DEFAULT_SEGMENT_COVERAGE_TARGET) -> list[int]:
    target = max(1, min(int(coverage_target), limit))
    values: list[int] = []
    current = 1
    while current < target:
        values.append(current)
        current *= 2
    values.append(target)
    return values


def _coverage_segment_priority(
    items: list[FeaturePair],
    *,
    available_days: set[str],
    available_runs: set[str],
    target: int = DEFAULT_SEGMENT_COVERAGE_TARGET,
) -> tuple[int, int, int, int, float, str]:
    if not items:
        return (0, 0, 0, 0, 0.0, "")
    windows = {_market_window_key(item[0].event_ts, DEFAULT_MARKET_WINDOW_HOURS) for item in items}
    latest_ts = max(_aware(item[0].event_ts).timestamp() for item in items)
    targetable = 1 if len(items) >= target else 0
    diversity_ready = 1 if len(available_days) >= 2 and len(windows) >= 2 and len(available_runs) >= 2 else 0
    return (
        targetable,
        diversity_ready,
        min(len(items), target),
        len(available_days) + len(windows) + len(available_runs),
        latest_ts,
        _segment_key(items[0][0]),
    )


def _coverage_sample_priority(item: FeaturePair) -> tuple[int, int, float]:
    event, _label, snapshot, collection_run = item
    has_run = 1 if (snapshot and snapshot.collection_run_id) or (collection_run and collection_run.id) else 0
    has_snapshot = 1 if snapshot is not None else 0
    return (has_run, has_snapshot, _aware(event.event_ts).timestamp())


async def _balanced_labeled_feature_rows(
    session: AsyncSession,
    *,
    horizon: str,
    limit: int,
    coverage_target: int = DEFAULT_SEGMENT_COVERAGE_TARGET,
) -> list[FeaturePair]:
    if limit <= 0:
        return []
    if session.get_bind().dialect.name == "postgresql":
        return await _bucketed_labeled_feature_rows_postgres(
            session,
            horizon=horizon,
            limit=limit,
            coverage_target=coverage_target,
        )
    days = await _labeled_event_days(session, horizon=horizon, limit=min(limit, DEFAULT_BALANCED_SAMPLE_DAYS))
    if not days:
        return []
    per_day_limit = max(1, (limit + len(days) - 1) // len(days))
    rows: list[FeaturePair] = []
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


async def _bucketed_labeled_feature_rows_postgres(
    session: AsyncSession,
    *,
    horizon: str,
    limit: int,
    coverage_target: int = DEFAULT_SEGMENT_COVERAGE_TARGET,
) -> list[FeaturePair]:
    days = await _labeled_event_days(session, horizon=horizon, limit=DEFAULT_BALANCED_SAMPLE_DAYS)
    if not days:
        return []
    rows: list[FeaturePair] = []
    seen_event_ids: set[str] = set()
    bucket_minutes = DEFAULT_DEDUPE_BUCKET_MINUTES
    max_buckets = max(int(coverage_target) * 8, (limit + 749) // 750)
    now = datetime.now(timezone.utc)
    scanned_buckets = 0
    for bucket_start in _iter_day_buckets(days, bucket_minutes=bucket_minutes):
        if bucket_start > now:
            continue
        scanned_buckets += 1
        bucket_end = bucket_start + timedelta(minutes=bucket_minutes)
        result = await session.execute(
            _labeled_feature_query(horizon)
            .where(
                FeatureEventModel.event_ts >= bucket_start,
                FeatureEventModel.event_ts < bucket_end,
                FeatureLabel.return_pct.isnot(None),
                FeatureEventModel.direction.in_(("long", "short")),
            )
            .distinct(
                FeatureEventModel.feature_name,
                FeatureEventModel.subtype,
                FeatureEventModel.direction,
                FeatureEventModel.symbol,
                FeatureEventModel.timeframe,
            )
            .order_by(
                FeatureEventModel.feature_name,
                FeatureEventModel.subtype,
                FeatureEventModel.direction,
                FeatureEventModel.symbol,
                FeatureEventModel.timeframe,
                SignalSnapshot.collection_run_id.isnot(None).desc(),
                FeatureEventModel.event_ts.desc(),
                FeatureEventModel.id.desc(),
            )
        )
        _append_unique_feature_rows(rows, result.all(), seen_event_ids=seen_event_ids)
        if len(rows) >= limit or scanned_buckets >= max_buckets:
            break
    if len(rows) < limit:
        fill_query = _labeled_feature_query(horizon).order_by(FeatureEventModel.event_ts.desc()).limit(limit - len(rows))
        if seen_event_ids:
            fill_query = fill_query.where(~FeatureEventModel.id.in_(seen_event_ids))
        result = await session.execute(fill_query)
        _append_unique_feature_rows(rows, result.all(), seen_event_ids=seen_event_ids)
    return sorted(rows, key=lambda row: _aware(row[0].event_ts), reverse=True)[:limit]


def _iter_day_buckets(days: list[Any], *, bucket_minutes: int) -> list[datetime]:
    buckets_per_day = max(1, (24 * 60) // max(1, int(bucket_minutes)))
    starts: list[datetime] = []
    for item in days:
        day = _aware(item).date() if isinstance(item, datetime) else item
        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        for index in range(buckets_per_day - 1, -1, -1):
            starts.append(day_start + timedelta(minutes=index * bucket_minutes))
    return starts


async def _labeled_event_days(
    session: AsyncSession,
    *,
    horizon: str,
    limit: int,
) -> list[Any]:
    day_expr = func.date(FeatureEventModel.event_ts)
    rows = await session.execute(
        select(day_expr)
        .select_from(FeatureEventModel)
        .join(FeatureLabel, FeatureLabel.feature_event_id == FeatureEventModel.id)
        .where(
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
        select(
            FeatureEventModel,
            FeatureLabel,
            SignalSnapshot.id,
            SignalSnapshot.collection_run_id,
            SignalSnapshot.collected_at,
            CollectionRun.id,
            CollectionRun.started_at,
            CollectionRun.finished_at,
        )
        .select_from(FeatureEventModel)
        .join(FeatureLabel, FeatureLabel.feature_event_id == FeatureEventModel.id)
        .outerjoin(SignalSnapshot, SignalSnapshot.id == FeatureEventModel.snapshot_id)
        .outerjoin(CollectionRun, CollectionRun.id == SignalSnapshot.collection_run_id)
        .where(
            FeatureLabel.horizon == horizon,
            FeatureLabel.status == "labeled",
        )
    )


def _append_unique_feature_rows(
    rows: list[FeaturePair],
    candidates: list[Any],
    *,
    seen_event_ids: set[str],
) -> None:
    for raw_item in candidates:
        item = _feature_pair_from_row(raw_item)
        event = item[0]
        if event.id in seen_event_ids:
            continue
        seen_event_ids.add(event.id)
        rows.append(item)


def _feature_pair_from_row(item: Any) -> FeaturePair:
    event = item[0]
    label = item[1]
    snapshot = None
    if item[2] is not None and item[4] is not None:
        snapshot = SnapshotRef(id=item[2], collection_run_id=item[3], collected_at=item[4])
    collection_run = None
    if item[5] is not None and item[6] is not None:
        collection_run = CollectionRunRef(id=item[5], started_at=item[6], finished_at=item[7])
    return event, label, snapshot, collection_run


async def _collection_run_windows(session: AsyncSession) -> list[CollectionRunRef]:
    rows = await session.execute(
        select(CollectionRun.id, CollectionRun.started_at, CollectionRun.finished_at)
        .order_by(CollectionRun.started_at.desc())
        .limit(5000)
    )
    return [CollectionRunRef(id=row[0], started_at=row[1], finished_at=row[2]) for row in rows.all()]


def _infer_collection_run(snapshot: SnapshotRef | None, collection_runs: list[CollectionRunRef]) -> CollectionRunRef | None:
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
        time_split = _time_split_validation(items, thresholds=thresholds)
        first = items[0][0]
        segment_report = _segment_report(items, thresholds=thresholds)
        reasons = _candidate_reasons(stats, segment_report, time_split=time_split, thresholds=thresholds)
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
                "avg_return_lower": stats["avg_return_lower"],
                "avg_return_upper": stats["avg_return_upper"],
                "median_return": stats["median_return"],
                "profit_factor": stats["profit_factor"],
                "profit_factor_lower": stats["profit_factor_lower"],
                "win_rate_lower": stats["win_rate_lower"],
                "win_count": stats["win_count"],
                "loss_count": stats["loss_count"],
                "reliability_score": stats["reliability_score"],
                "avg_mfe": stats["avg_mfe"],
                "avg_mae": stats["avg_mae"],
                "avg_strength": _avg_strength(items),
                "time_split": time_split,
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
            row["reliability_score"] if row["reliability_score"] is not None else -999.0,
            row["profit_factor_lower"] if row["profit_factor_lower"] is not None else -999.0,
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
        time_split = _time_split_validation(effective_items, thresholds=thresholds)
        quality = _sample_quality(items, effective_items, thresholds=thresholds)
        first = items[0][0]
        reasons = _segment_candidate_reasons(stats, quality=quality, time_split=time_split, thresholds=thresholds)
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
                "avg_return_lower": stats["avg_return_lower"],
                "avg_return_upper": stats["avg_return_upper"],
                "median_return": stats["median_return"],
                "profit_factor": stats["profit_factor"],
                "profit_factor_lower": stats["profit_factor_lower"],
                "win_rate_lower": stats["win_rate_lower"],
                "win_count": stats["win_count"],
                "loss_count": stats["loss_count"],
                "reliability_score": stats["reliability_score"],
                "avg_mfe": stats["avg_mfe"],
                "avg_mae": stats["avg_mae"],
                "avg_strength": _avg_strength(effective_items),
                "quality": quality,
                "time_split": time_split,
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
            row["time_split"].get("status") == "passed",
            row["reliability_score"] if row["reliability_score"] is not None else -999.0,
            row["profit_factor_lower"] if row["profit_factor_lower"] is not None else -999.0,
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
    time_split: dict[str, Any] | None = None,
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
    if _uses_reliable_profit_factor(stats, thresholds=thresholds):
        reasons.append("profit_factor_lower_below_minimum")
    if time_split and time_split.get("status") in {"failed_validation", "decayed"}:
        reasons.append(str(time_split["status"]))
    if segment_report["segment_count"] < thresholds["min_segments"]:
        reasons.append("segment_count_below_minimum")
    if segment_report["weak_segments"]:
        reasons.append("weak_segments")
    return reasons


def _segment_candidate_reasons(
    stats: dict[str, Any],
    *,
    quality: dict[str, Any] | None = None,
    time_split: dict[str, Any] | None = None,
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
    if _uses_reliable_profit_factor(stats, thresholds=thresholds):
        reasons.append("profit_factor_lower_below_minimum")
    if time_split and time_split.get("status") in {"failed_validation", "decayed"}:
        reasons.append(str(time_split["status"]))
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
    avg_return = mean(values) if values else None
    profit_factor = gross_win / gross_loss if gross_loss else (999.0 if gross_win else None)
    profit_factor_lower = _profit_factor_lower_bound(wins, losses)
    win_rate = len(wins) / len(values) if values else None
    avg_lower, avg_upper = _mean_confidence_bounds(values)
    return {
        "trade_count": len(values),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": win_rate,
        "win_rate_lower": _wilson_lower_bound(len(wins), len(values)) if values else None,
        "avg_return": avg_return,
        "avg_return_lower": avg_lower,
        "avg_return_upper": avg_upper,
        "median_return": _median(values),
        "profit_factor": profit_factor,
        "profit_factor_lower": profit_factor_lower,
        "reliability_score": _reliability_score(
            trade_count=len(values),
            avg_return_lower=avg_lower,
            profit_factor_lower=profit_factor_lower,
            win_rate_lower=_wilson_lower_bound(len(wins), len(values)) if values else None,
        ),
        "avg_mfe": _avg_label(items, "mfe"),
        "avg_mae": _avg_label(items, "mae"),
        "gross_win": gross_win,
        "gross_loss": gross_loss,
    }


def _time_split_validation(items: list[FeaturePair], *, thresholds: dict[str, Any]) -> dict[str, Any]:
    ordered = sorted(items, key=lambda item: _aware(item[0].event_ts))
    sample_count = len([item for item in ordered if isinstance(item[1].return_pct, (int, float))])
    min_samples = max(int(thresholds.get("time_split_min_samples") or DEFAULT_TIME_SPLIT_MIN_SAMPLES), int(thresholds["min_samples"]))
    if sample_count < min_samples:
        return {"status": "insufficient", "sample_count": sample_count, "min_samples": min_samples, "splits": {}}

    train_end = max(1, int(sample_count * 0.7))
    validation_end = max(train_end + 1, int(sample_count * 0.9))
    validation_end = min(validation_end, sample_count - 1) if sample_count >= 3 else validation_end
    splits = {
        "train": _pseudo_trade_stats(ordered[:train_end]),
        "validation": _pseudo_trade_stats(ordered[train_end:validation_end]),
        "recent": _pseudo_trade_stats(ordered[validation_end:]),
    }
    validation = splits["validation"]
    recent = splits["recent"]
    status = "passed"
    if _split_is_below_threshold(validation, thresholds=thresholds):
        status = "failed_validation"
    elif _split_is_below_threshold(recent, thresholds=thresholds):
        status = "decayed"
    return {
        "status": status,
        "sample_count": sample_count,
        "min_samples": min_samples,
        "split_ratios": {"train": 0.7, "validation": 0.2, "recent": 0.1},
        "splits": splits,
    }


def _split_is_below_threshold(stats: dict[str, Any], *, thresholds: dict[str, Any]) -> bool:
    if not stats["trade_count"]:
        return True
    if stats["avg_return"] is None or stats["avg_return"] <= float(thresholds["min_avg_return"]):
        return True
    if stats["profit_factor"] is None or stats["profit_factor"] < float(thresholds["min_profit_factor"]):
        return True
    return False


def _uses_reliable_profit_factor(stats: dict[str, Any], *, thresholds: dict[str, Any]) -> bool:
    minimum = float(thresholds.get("min_profit_factor_lower") or DEFAULT_MIN_PROFIT_FACTOR_LOWER)
    if minimum <= 0:
        return False
    if stats["trade_count"] < max(int(thresholds["min_samples"]), DEFAULT_TIME_SPLIT_MIN_SAMPLES):
        return False
    lower = stats.get("profit_factor_lower")
    return lower is None or float(lower) < minimum


def _wilson_lower_bound(wins: int, total: int, *, z: float = CONFIDENCE_Z) -> float | None:
    if total <= 0:
        return None
    p = wins / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denominator)


def _mean_confidence_bounds(values: list[float], *, z: float = CONFIDENCE_Z) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    avg = mean(values)
    if len(values) < 2:
        return avg, avg
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    margin = z * sqrt(variance / len(values))
    return avg - margin, avg + margin


def _profit_factor_lower_bound(wins: list[float], losses: list[float]) -> float | None:
    if not wins and not losses:
        return None
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    avg_win = mean(wins) if wins else 0.0
    avg_loss = abs(mean(losses)) if losses else 0.0
    sample_count = len(wins) + len(losses)
    win_shrink = len(wins) / (len(wins) + 2) if wins else 0.0
    adjusted_win = max(gross_win * win_shrink, 0.0)
    pseudo_loss = max(avg_loss, avg_win, abs(gross_win) / max(sample_count, 1), 1e-12)
    adjusted_loss = gross_loss + pseudo_loss * 2
    return adjusted_win / adjusted_loss if adjusted_loss else None


def _reliability_score(
    *,
    trade_count: int,
    avg_return_lower: float | None,
    profit_factor_lower: float | None,
    win_rate_lower: float | None,
) -> float | None:
    if trade_count <= 0:
        return None
    sample_factor = min(trade_count / DEFAULT_TIME_SPLIT_MIN_SAMPLES, 1.0)
    edge_score = (
        min(trade_count / 100.0, 1.0) * 0.25
        + max(avg_return_lower or 0.0, 0.0) * 25.0
        + min(max((profit_factor_lower or 0.0) / 2.0, 0.0), 1.0) * 0.35
        + min(max(win_rate_lower or 0.0, 0.0), 1.0) * 0.15
    )
    return sample_factor * edge_score


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
    min_profit_factor_lower: float = DEFAULT_MIN_PROFIT_FACTOR_LOWER,
    time_split_min_samples: int = DEFAULT_TIME_SPLIT_MIN_SAMPLES,
) -> dict[str, Any]:
    return {
        "min_samples": int(min_samples),
        "min_win_rate": float(min_win_rate),
        "min_profit_factor": float(min_profit_factor),
        "min_profit_factor_lower": float(min_profit_factor_lower),
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
        "time_split_min_samples": max(1, int(time_split_min_samples)),
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
                "weighted_profit_factor_lower": _weighted_mean(items, "profit_factor_lower", "sample_count"),
                "weighted_reliability_score": _weighted_mean(items, "reliability_score", "sample_count"),
                "overfit_risk": _combined_overfit_risk(items),
                "segments": [row["symbol_timeframe"] for row in items[:12]],
                "used_for_execution_weights": False,
                "used_for_opening_decisions": False,
            }
        )
    return sorted(
        summary,
        key=lambda row: (
            row["weighted_reliability_score"] or -999.0,
            row["weighted_profit_factor_lower"] or -999.0,
            row["weighted_avg_return"] or -999.0,
            row["weighted_win_rate"] or 0.0,
            row["sample_count"],
        ),
        reverse=True,
    )


async def _latest_persisted_report(
    session: AsyncSession,
    *,
    name: str,
    empty_factory: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    item = await session.scalar(
        select(ExperimentRun)
        .where(ExperimentRun.name == name, ExperimentRun.status == "research")
        .order_by(ExperimentRun.created_at.desc())
        .limit(1)
    )
    if item is None:
        report = empty_factory()
        report["materialized"] = False
        report["generated_at"] = None
        report["stale_seconds"] = None
        report["source_experiment_run_id"] = None
        report["experiment_run"] = None
        return report
    report = copy.deepcopy(item.metrics or empty_factory())
    report["materialized"] = True
    report["generated_at"] = item.created_at
    report["stale_seconds"] = max(0.0, (_aware(datetime.now(timezone.utc)) - _aware(item.created_at)).total_seconds())
    report["source_experiment_run_id"] = item.id
    report["experiment_run"] = {"id": item.id, "name": item.name, "status": item.status}
    return report


def _empty_feature_candidate_report(*, horizon: str) -> dict[str, Any]:
    return {
        "horizon": horizon,
        "limit": 0,
        "labeled_count": 0,
        "feature_group_count": 0,
        "candidate_count": 0,
        "watchlist_count": 0,
        "thresholds": {},
        "policy": _research_policy(),
        "candidates": [],
        "watchlist": [],
        "rejected_summary": {},
        "all_features": [],
        "empty_reason": "no_materialized_report",
    }


def _empty_feature_paper_ab_report(*, horizon: str) -> dict[str, Any]:
    return {
        "horizon": horizon,
        "limit": 0,
        "candidate_limit": 0,
        "selected_candidate_count": 0,
        "selected_feature_keys": [],
        "thresholds": {},
        "policy": _research_policy(),
        "data_quality": {"labeled_count": 0, "status": "no_materialized_report"},
        "arms": {},
        "per_candidate": [],
        "candidate_screen": {},
        "empty_reason": "no_materialized_report",
    }


def _empty_feature_segment_candidate_report(*, horizon: str) -> dict[str, Any]:
    return {
        "horizon": horizon,
        "limit": 0,
        "labeled_count": 0,
        "segment_group_count": 0,
        "candidate_count": 0,
        "thresholds": {},
        "policy": _research_policy(),
        "quality_summary": {},
        "risk_summary": {},
        "candidates": [],
        "rejected_summary": {},
        "by_feature": [],
        "all_segments": [],
        "empty_reason": "no_materialized_report",
    }


def _empty_feature_segment_paper_ab_report(*, horizon: str) -> dict[str, Any]:
    return {
        "horizon": horizon,
        "limit": 0,
        "candidate_limit": 0,
        "selected_candidate_count": 0,
        "selected_segment_keys": [],
        "thresholds": {},
        "policy": _research_policy(),
        "data_quality": {"labeled_count": 0, "status": "no_materialized_report"},
        "quality": {},
        "arms": {},
        "per_segment": [],
        "segment_screen": {},
        "empty_reason": "no_materialized_report",
    }


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    candidate_count = report.get("candidate_count")
    if candidate_count is None:
        candidate_count = report.get("selected_candidate_count")
    return {
        "horizon": report.get("horizon"),
        "requested_limit": report.get("requested_limit"),
        "limit": report.get("limit"),
        "limit_capped": report.get("limit_capped"),
        "labeled_count": report.get("labeled_count") or (report.get("data_quality") or {}).get("labeled_count"),
        "candidate_count": candidate_count,
        "experiment_run": report.get("experiment_run"),
    }


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
    snapshot: SnapshotRef | None,
    collection_run: CollectionRunRef | None,
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
