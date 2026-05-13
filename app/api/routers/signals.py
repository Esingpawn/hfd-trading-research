from __future__ import annotations

import copy
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.services.experiment_effectiveness import experiment_feature_effectiveness
from app.services.feature_candidates import (
    feature_candidate_screen,
    feature_paper_ab,
    feature_segment_candidate_screen,
    feature_segment_paper_ab,
)
from app.services.features import (
    backfill_feature_events,
    backfill_feature_labels,
    feature_effectiveness,
    reset_feature_research,
    refresh_feature_research,
)
from app.services.indicator_catalog import indicator_experiment_coverage
from app.services.signal_attribution import backfill_signal_outcomes, signal_effectiveness
from app.services.signal_weights import signal_weight_governance

router = APIRouter()

_REPORT_CACHE_SECONDS = 300.0
_REPORT_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, object]]] = {}


@router.post("/signals/backfill")
async def backfill_signals(
    session: SessionDep,
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, object]:
    result = await backfill_signal_outcomes(session, limit=limit)
    return result.__dict__


@router.get("/signals/effectiveness")
async def signals_effectiveness(
    session: SessionDep,
    min_samples: int = Query(default=1, ge=1, le=1000),
    horizon: str = Query(default="4h", pattern="^(30m|1h|4h|24h)$"),
) -> dict[str, object]:
    return await signal_effectiveness(session, min_samples=min_samples, horizon=horizon)


@router.get("/signals/weights")
async def signals_weights(
    session: SessionDep,
    min_samples: int = Query(default=30, ge=1, le=1000),
    horizon: str = Query(default="4h", pattern="^(30m|1h|4h|24h)$"),
) -> dict[str, object]:
    return await signal_weight_governance(session, min_samples=min_samples, horizon=horizon)


@router.get("/signals/experiments")
async def signals_experiments(session: SessionDep) -> dict[str, object]:
    return await indicator_experiment_coverage(session)


@router.get("/signals/experiment-effectiveness")
async def signals_experiment_effectiveness(
    session: SessionDep,
    min_samples: int = Query(default=5, ge=1, le=500),
    horizon: str = Query(default="4h", pattern="^(30m|1h|4h|24h)$"),
    limit_per_series: int = Query(default=80, ge=10, le=500),
) -> dict[str, object]:
    return await experiment_feature_effectiveness(
        session,
        horizon=horizon,
        min_samples=min_samples,
        limit_per_series=limit_per_series,
    )


@router.post("/features/backfill")
async def backfill_features(
    session: SessionDep,
    limit: int = Query(default=500, ge=1, le=5000),
    indicators: list[str] | None = Query(default=None),
) -> dict[str, object]:
    result = await backfill_feature_events(session, limit=limit, indicators=indicators)
    return result.__dict__


@router.post("/features/labels/backfill")
async def backfill_feature_label_rows(
    session: SessionDep,
    limit: int = Query(default=1000, ge=1, le=20000),
    horizons: list[str] | None = Query(default=None),
    refresh_labeled: bool = Query(default=False),
) -> dict[str, object]:
    result = await backfill_feature_labels(
        session,
        limit=limit,
        horizons=horizons,
        refresh_labeled=refresh_labeled,
    )
    return result.__dict__


@router.post("/features/reset")
async def reset_features(
    session: SessionDep,
    indicators: list[str] | None = Query(default=None),
) -> dict[str, object]:
    result = await reset_feature_research(session, indicators=indicators)
    return result.__dict__


@router.post("/features/refresh")
async def refresh_features(
    session: SessionDep,
    limit: int = Query(default=500, ge=1, le=5000),
    indicators: list[str] | None = Query(default=None),
    horizons: list[str] | None = Query(default=None),
    min_samples: int = Query(default=5, ge=1, le=1000),
) -> dict[str, object]:
    return await refresh_feature_research(
        session,
        limit=limit,
        indicators=indicators,
        horizons=horizons,
        min_samples=min_samples,
    )


@router.get("/features/effectiveness")
async def features_effectiveness(
    session: SessionDep,
    min_samples: int = Query(default=5, ge=1, le=1000),
    horizon: str = Query(default="4h", pattern="^(30m|1h|4h|24h)$"),
    limit: int = Query(default=10000, ge=1, le=50000),
) -> dict[str, object]:
    return await feature_effectiveness(session, min_samples=min_samples, horizon=horizon, limit=limit)


@router.get("/features/candidates")
async def feature_candidates(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
    min_samples: int = Query(default=30, ge=1, le=5000),
    min_win_rate: float = Query(default=0.52, ge=0.0, le=1.0),
    min_profit_factor: float = Query(default=1.2, ge=0.0, le=1000.0),
    min_avg_return: float = Query(default=0.0, ge=-1.0, le=1.0),
    segment_min_samples: int = Query(default=5, ge=1, le=1000),
    min_segments: int = Query(default=2, ge=1, le=100),
    limit: int = Query(default=20000, ge=1, le=100000),
) -> dict[str, object]:
    return await _cached_report(
        "feature_candidates",
        horizon,
        min_samples,
        min_win_rate,
        min_profit_factor,
        min_avg_return,
        segment_min_samples,
        min_segments,
        limit,
        compute=lambda: feature_candidate_screen(
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
        ),
    )


@router.post("/features/candidates")
async def persist_feature_candidates(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
    min_samples: int = Query(default=30, ge=1, le=5000),
    min_win_rate: float = Query(default=0.52, ge=0.0, le=1.0),
    min_profit_factor: float = Query(default=1.2, ge=0.0, le=1000.0),
    min_avg_return: float = Query(default=0.0, ge=-1.0, le=1.0),
    segment_min_samples: int = Query(default=5, ge=1, le=1000),
    min_segments: int = Query(default=2, ge=1, le=100),
    limit: int = Query(default=20000, ge=1, le=100000),
) -> dict[str, object]:
    return await feature_candidate_screen(
        session,
        horizon=horizon,
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_avg_return=min_avg_return,
        segment_min_samples=segment_min_samples,
        min_segments=min_segments,
        limit=limit,
        persist=True,
    )


@router.get("/features/paper-ab")
async def feature_paper_ab_report(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
    min_samples: int = Query(default=30, ge=1, le=5000),
    min_win_rate: float = Query(default=0.52, ge=0.0, le=1.0),
    min_profit_factor: float = Query(default=1.2, ge=0.0, le=1000.0),
    min_avg_return: float = Query(default=0.0, ge=-1.0, le=1.0),
    segment_min_samples: int = Query(default=5, ge=1, le=1000),
    min_segments: int = Query(default=2, ge=1, le=100),
    candidate_limit: int = Query(default=20, ge=1, le=500),
    limit: int = Query(default=20000, ge=1, le=100000),
) -> dict[str, object]:
    return await _cached_report(
        "feature_paper_ab",
        horizon,
        min_samples,
        min_win_rate,
        min_profit_factor,
        min_avg_return,
        segment_min_samples,
        min_segments,
        candidate_limit,
        limit,
        compute=lambda: feature_paper_ab(
            session,
            horizon=horizon,
            min_samples=min_samples,
            min_win_rate=min_win_rate,
            min_profit_factor=min_profit_factor,
            min_avg_return=min_avg_return,
            segment_min_samples=segment_min_samples,
            min_segments=min_segments,
            candidate_limit=candidate_limit,
            limit=limit,
            persist=False,
        ),
    )


@router.post("/features/paper-ab")
async def persist_feature_paper_ab_report(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
    min_samples: int = Query(default=30, ge=1, le=5000),
    min_win_rate: float = Query(default=0.52, ge=0.0, le=1.0),
    min_profit_factor: float = Query(default=1.2, ge=0.0, le=1000.0),
    min_avg_return: float = Query(default=0.0, ge=-1.0, le=1.0),
    segment_min_samples: int = Query(default=5, ge=1, le=1000),
    min_segments: int = Query(default=2, ge=1, le=100),
    candidate_limit: int = Query(default=20, ge=1, le=500),
    limit: int = Query(default=20000, ge=1, le=100000),
) -> dict[str, object]:
    return await feature_paper_ab(
        session,
        horizon=horizon,
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_avg_return=min_avg_return,
        segment_min_samples=segment_min_samples,
        min_segments=min_segments,
        candidate_limit=candidate_limit,
        limit=limit,
        persist=True,
    )


@router.get("/features/segment-candidates")
async def feature_segment_candidates(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
    min_samples: int = Query(default=30, ge=1, le=5000),
    min_win_rate: float = Query(default=0.52, ge=0.0, le=1.0),
    min_profit_factor: float = Query(default=1.2, ge=0.0, le=1000.0),
    min_avg_return: float = Query(default=0.0, ge=-1.0, le=1.0),
    dedupe_research_samples: bool = Query(default=True),
    dedupe_bucket_minutes: int = Query(default=30, ge=1, le=1440),
    min_unique_time_buckets: int = Query(default=3, ge=1, le=5000),
    min_unique_event_days: int = Query(default=2, ge=1, le=365),
    min_unique_market_windows: int = Query(default=2, ge=1, le=5000),
    min_unique_collection_runs: int = Query(default=2, ge=1, le=5000),
    market_window_hours: int = Query(default=8, ge=1, le=24),
    max_same_return_samples: int = Query(default=10, ge=1, le=5000),
    max_return_cluster_ratio: float = Query(default=0.75, ge=0.0, le=1.0),
    limit: int = Query(default=20000, ge=1, le=100000),
) -> dict[str, object]:
    return await _cached_report(
        "feature_segment_candidates",
        horizon,
        min_samples,
        min_win_rate,
        min_profit_factor,
        min_avg_return,
        dedupe_research_samples,
        dedupe_bucket_minutes,
        min_unique_time_buckets,
        min_unique_event_days,
        min_unique_market_windows,
        min_unique_collection_runs,
        market_window_hours,
        max_same_return_samples,
        max_return_cluster_ratio,
        limit,
        compute=lambda: feature_segment_candidate_screen(
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
        ),
    )


@router.post("/features/segment-candidates")
async def persist_feature_segment_candidates(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
    min_samples: int = Query(default=30, ge=1, le=5000),
    min_win_rate: float = Query(default=0.52, ge=0.0, le=1.0),
    min_profit_factor: float = Query(default=1.2, ge=0.0, le=1000.0),
    min_avg_return: float = Query(default=0.0, ge=-1.0, le=1.0),
    dedupe_research_samples: bool = Query(default=True),
    dedupe_bucket_minutes: int = Query(default=30, ge=1, le=1440),
    min_unique_time_buckets: int = Query(default=3, ge=1, le=5000),
    min_unique_event_days: int = Query(default=2, ge=1, le=365),
    min_unique_market_windows: int = Query(default=2, ge=1, le=5000),
    min_unique_collection_runs: int = Query(default=2, ge=1, le=5000),
    market_window_hours: int = Query(default=8, ge=1, le=24),
    max_same_return_samples: int = Query(default=10, ge=1, le=5000),
    max_return_cluster_ratio: float = Query(default=0.75, ge=0.0, le=1.0),
    limit: int = Query(default=20000, ge=1, le=100000),
) -> dict[str, object]:
    return await feature_segment_candidate_screen(
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
        persist=True,
    )


@router.get("/features/segment-paper-ab")
async def feature_segment_paper_ab_report(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
    min_samples: int = Query(default=30, ge=1, le=5000),
    min_win_rate: float = Query(default=0.52, ge=0.0, le=1.0),
    min_profit_factor: float = Query(default=1.2, ge=0.0, le=1000.0),
    min_avg_return: float = Query(default=0.0, ge=-1.0, le=1.0),
    dedupe_research_samples: bool = Query(default=True),
    dedupe_bucket_minutes: int = Query(default=30, ge=1, le=1440),
    min_unique_time_buckets: int = Query(default=3, ge=1, le=5000),
    min_unique_event_days: int = Query(default=2, ge=1, le=365),
    min_unique_market_windows: int = Query(default=2, ge=1, le=5000),
    min_unique_collection_runs: int = Query(default=2, ge=1, le=5000),
    market_window_hours: int = Query(default=8, ge=1, le=24),
    max_same_return_samples: int = Query(default=10, ge=1, le=5000),
    max_return_cluster_ratio: float = Query(default=0.75, ge=0.0, le=1.0),
    candidate_limit: int = Query(default=50, ge=1, le=500),
    limit: int = Query(default=20000, ge=1, le=100000),
) -> dict[str, object]:
    return await _cached_report(
        "feature_segment_paper_ab",
        horizon,
        min_samples,
        min_win_rate,
        min_profit_factor,
        min_avg_return,
        dedupe_research_samples,
        dedupe_bucket_minutes,
        min_unique_time_buckets,
        min_unique_event_days,
        min_unique_market_windows,
        min_unique_collection_runs,
        market_window_hours,
        max_same_return_samples,
        max_return_cluster_ratio,
        candidate_limit,
        limit,
        compute=lambda: feature_segment_paper_ab(
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
            candidate_limit=candidate_limit,
            limit=limit,
            persist=False,
        ),
    )


@router.post("/features/segment-paper-ab")
async def persist_feature_segment_paper_ab_report(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
    min_samples: int = Query(default=30, ge=1, le=5000),
    min_win_rate: float = Query(default=0.52, ge=0.0, le=1.0),
    min_profit_factor: float = Query(default=1.2, ge=0.0, le=1000.0),
    min_avg_return: float = Query(default=0.0, ge=-1.0, le=1.0),
    dedupe_research_samples: bool = Query(default=True),
    dedupe_bucket_minutes: int = Query(default=30, ge=1, le=1440),
    min_unique_time_buckets: int = Query(default=3, ge=1, le=5000),
    min_unique_event_days: int = Query(default=2, ge=1, le=365),
    min_unique_market_windows: int = Query(default=2, ge=1, le=5000),
    min_unique_collection_runs: int = Query(default=2, ge=1, le=5000),
    market_window_hours: int = Query(default=8, ge=1, le=24),
    max_same_return_samples: int = Query(default=10, ge=1, le=5000),
    max_return_cluster_ratio: float = Query(default=0.75, ge=0.0, le=1.0),
    candidate_limit: int = Query(default=50, ge=1, le=500),
    limit: int = Query(default=20000, ge=1, le=100000),
) -> dict[str, object]:
    return await feature_segment_paper_ab(
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
        candidate_limit=candidate_limit,
        limit=limit,
        persist=True,
    )


async def _cached_report(
    *parts: Any,
    compute: Callable[[], Awaitable[dict[str, object]]],
) -> dict[str, object]:
    key = tuple(_cache_part(part) for part in parts)
    now = time.monotonic()
    cached = _REPORT_CACHE.get(key)
    if cached is not None:
        created_at, payload = cached
        if now - created_at <= _REPORT_CACHE_SECONDS:
            return copy.deepcopy({**payload, "cache": _cache_meta(created_at, hit=True)})
    payload = await compute()
    _REPORT_CACHE[key] = (now, copy.deepcopy(payload))
    return {**payload, "cache": _cache_meta(now, hit=False)}


def _cache_meta(created_at: float, *, hit: bool) -> dict[str, object]:
    age = max(0.0, time.monotonic() - created_at)
    return {
        "hit": hit,
        "age_seconds": round(age, 3),
        "ttl_seconds": _REPORT_CACHE_SECONDS,
        "remaining_seconds": max(0.0, round(_REPORT_CACHE_SECONDS - age, 3)),
    }


def _cache_part(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 8)
    return value
