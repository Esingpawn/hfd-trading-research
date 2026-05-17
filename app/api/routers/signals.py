from __future__ import annotations

import copy
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.application.tasks import enqueue_task
from app.models import TaskRun
from app.services.experiment_effectiveness import experiment_feature_effectiveness
from app.services.feature_candidates import (
    DEFAULT_RESEARCH_QUERY_MAX_LIMIT,
    feature_candidate_screen,
    feature_paper_ab,
    feature_segment_candidate_screen,
    feature_segment_paper_ab,
    latest_feature_candidate_screen,
    latest_feature_paper_ab,
    latest_feature_segment_candidate_screen,
    latest_feature_segment_paper_ab,
    research_query_max_limit,
)
from app.services.features import (
    backfill_feature_events,
    backfill_feature_labels,
    feature_effectiveness,
    reset_feature_research,
    refresh_feature_research,
)
from app.services.darkflow_playbooks import (
    darkflow_playbook_backtest,
    darkflow_playbook_catalog,
    latest_darkflow_playbook_backtest,
)
from app.services.darkflow_interactions import (
    backfill_darkflow_interactions,
    backfill_darkflow_zones,
    darkflow_interaction_backtest,
    darkflow_shadow_replay,
    latest_darkflow_interaction_backtest,
)
from app.services.darkflow_decision_cards import (
    latest_darkflow_decision_cards,
    latest_materialized_trade_candidates,
    materialize_darkflow_trade_candidates,
)
from app.services.darkflow_candidate_promotion import (
    audit_darkflow_trade_candidates,
    darkflow_entry_plan_state_report,
    darkflow_candidate_promotion_report,
    open_darkflow_shadow_forward_samples,
    refresh_darkflow_candidate_promotion,
)
from app.services.darkflow_rules import darkflow_rulebook
from app.services.indicator_catalog import indicator_experiment_coverage
from app.services.signal_attribution import backfill_signal_outcomes, signal_effectiveness
from app.services.signal_weights import signal_weight_governance

router = APIRouter()

_REPORT_CACHE_SECONDS = 300.0
_REPORT_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, object]]] = {}
_RESEARCH_REFRESH_ACTIVE_STATUSES = {"queued", "recorded", "running"}
_RESEARCH_REFRESH_QUEUED_GRACE_SECONDS = 120
_RESEARCH_REFRESH_RUNNING_GRACE_SECONDS = 300


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


@router.get("/darkflow/rulebook")
async def darkflow_rulebook_report() -> dict[str, object]:
    return darkflow_rulebook()


@router.get("/darkflow/playbooks")
async def darkflow_playbook_catalog_report() -> dict[str, object]:
    return darkflow_playbook_catalog()


@router.get("/darkflow/playbooks/backtest/latest")
async def latest_darkflow_playbook_backtest_report(
    session: SessionDep,
    horizon: str = Query(default="4h", pattern="^(30m|1h|4h|24h)$"),
) -> dict[str, object]:
    return await latest_darkflow_playbook_backtest(session, horizon=horizon)


@router.get("/darkflow/playbooks/backtest")
async def darkflow_playbook_backtest_report(
    session: SessionDep,
    horizon: str = Query(default="4h", pattern="^(30m|1h|4h|24h)$"),
    limit: int = Query(default=5000, ge=1, le=100000),
    min_samples: int = Query(default=30, ge=1, le=5000),
    min_win_rate: float = Query(default=0.52, ge=0.0, le=1.0),
    min_profit_factor: float = Query(default=1.1, ge=0.0, le=1000.0),
    min_avg_return: float = Query(default=0.0, ge=-1.0, le=1.0),
    confirmation_window_minutes: int = Query(default=90, ge=1, le=1440),
) -> dict[str, object]:
    return await darkflow_playbook_backtest(
        session,
        horizon=horizon,
        limit=limit,
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_avg_return=min_avg_return,
        confirmation_window_minutes=confirmation_window_minutes,
        persist=False,
    )


@router.post("/darkflow/playbooks/backtest")
async def persist_darkflow_playbook_backtest_report(
    session: SessionDep,
    horizon: str = Query(default="4h", pattern="^(30m|1h|4h|24h)$"),
    limit: int = Query(default=5000, ge=1, le=100000),
    min_samples: int = Query(default=30, ge=1, le=5000),
    min_win_rate: float = Query(default=0.52, ge=0.0, le=1.0),
    min_profit_factor: float = Query(default=1.1, ge=0.0, le=1000.0),
    min_avg_return: float = Query(default=0.0, ge=-1.0, le=1.0),
    confirmation_window_minutes: int = Query(default=90, ge=1, le=1440),
) -> dict[str, object]:
    return await darkflow_playbook_backtest(
        session,
        horizon=horizon,
        limit=limit,
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_avg_return=min_avg_return,
        confirmation_window_minutes=confirmation_window_minutes,
        persist=True,
    )


@router.post("/darkflow/zones/backfill")
async def backfill_darkflow_zone_rows(
    session: SessionDep,
    limit: int = Query(default=500, ge=1, le=100000),
    indicators: list[str] | None = Query(default=None),
    max_zones_per_snapshot: int = Query(default=120, ge=1, le=1000),
) -> dict[str, object]:
    result = await backfill_darkflow_zones(
        session,
        limit=limit,
        indicators=indicators,
        max_zones_per_snapshot=max_zones_per_snapshot,
    )
    return result.__dict__


@router.post("/darkflow/interactions/backfill")
async def backfill_darkflow_interaction_rows(
    session: SessionDep,
    limit: int = Query(default=500, ge=1, le=100000),
    indicators: list[str] | None = Query(default=None),
    max_zones_per_snapshot: int = Query(default=120, ge=1, le=1000),
    max_interactions_per_snapshot: int = Query(default=80, ge=1, le=1000),
    max_hold_bars: int = Query(default=12, ge=1, le=200),
) -> dict[str, object]:
    result = await backfill_darkflow_interactions(
        session,
        limit=limit,
        indicators=indicators,
        max_zones_per_snapshot=max_zones_per_snapshot,
        max_interactions_per_snapshot=max_interactions_per_snapshot,
        max_hold_bars=max_hold_bars,
    )
    return result.__dict__


@router.get("/darkflow/interactions/backtest/latest")
async def latest_darkflow_interaction_backtest_report(session: SessionDep) -> dict[str, object]:
    return await latest_darkflow_interaction_backtest(session)


@router.get("/darkflow/decision-cards")
async def latest_darkflow_decision_card_report(
    session: SessionDep,
    limit: int = Query(default=20, ge=1, le=100),
    min_quality_score: float = Query(default=55.0, ge=0.0, le=100.0),
    min_rr_ratio: float = Query(default=1.5, ge=0.0, le=20.0),
) -> dict[str, object]:
    return await latest_darkflow_decision_cards(
        session,
        limit=limit,
        min_quality_score=min_quality_score,
        min_rr_ratio=min_rr_ratio,
    )


@router.get("/darkflow/trade-candidates")
async def latest_darkflow_trade_candidates_report(
    session: SessionDep,
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    return await latest_materialized_trade_candidates(session, limit=limit)


@router.post("/darkflow/trade-candidates/materialize")
async def materialize_darkflow_trade_candidates_report(
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=1000),
    min_quality_score: float = Query(default=55.0, ge=0.0, le=100.0),
    min_rr_ratio: float = Query(default=1.5, ge=0.0, le=20.0),
) -> dict[str, object]:
    return await materialize_darkflow_trade_candidates(
        session,
        limit=limit,
        min_quality_score=min_quality_score,
        min_rr_ratio=min_rr_ratio,
    )


@router.get("/darkflow/trade-candidates/promotion")
async def darkflow_trade_candidate_promotion_report(
    session: SessionDep,
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, object]:
    return await darkflow_candidate_promotion_report(session, limit=limit)


@router.get("/darkflow/trade-candidates/entry-plan-states")
async def darkflow_trade_candidate_entry_plan_state_report(
    session: SessionDep,
    limit: int = Query(default=500, ge=1, le=5000),
    entry_tolerance_pct: float = Query(default=0.025, ge=0.0, le=0.2),
) -> dict[str, object]:
    return await darkflow_entry_plan_state_report(
        session,
        limit=limit,
        entry_tolerance_pct=entry_tolerance_pct,
    )


@router.post("/darkflow/trade-candidates/audit")
async def audit_darkflow_trade_candidates_report(
    session: SessionDep,
    limit: int = Query(default=500, ge=1, le=5000),
    include_blocked: bool = Query(default=False),
) -> dict[str, object]:
    return await audit_darkflow_trade_candidates(session, limit=limit, include_blocked=include_blocked)


@router.post("/darkflow/trade-candidates/shadow-forward")
async def open_darkflow_shadow_forward_report(
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=1000),
    max_candidate_age_hours: float = Query(default=72.0, ge=0.0, le=720.0),
    entry_tolerance_pct: float = Query(default=0.025, ge=0.0, le=0.2),
) -> dict[str, object]:
    return await open_darkflow_shadow_forward_samples(
        session,
        limit=limit,
        max_candidate_age_hours=max_candidate_age_hours,
        entry_tolerance_pct=entry_tolerance_pct,
    )


@router.post("/darkflow/trade-candidates/promotion/refresh")
async def refresh_darkflow_trade_candidate_promotion_report(
    session: SessionDep,
    limit: int = Query(default=500, ge=1, le=5000),
    shadow_limit: int = Query(default=100, ge=1, le=1000),
    max_candidate_age_hours: float = Query(default=72.0, ge=0.0, le=720.0),
    entry_tolerance_pct: float = Query(default=0.025, ge=0.0, le=0.2),
    materialize: bool = Query(default=True),
) -> dict[str, object]:
    return await refresh_darkflow_candidate_promotion(
        session,
        limit=limit,
        shadow_limit=shadow_limit,
        max_candidate_age_hours=max_candidate_age_hours,
        entry_tolerance_pct=entry_tolerance_pct,
        materialize=materialize,
    )


@router.get("/darkflow/interactions/backtest")
async def darkflow_interaction_backtest_report(
    session: SessionDep,
    limit: int = Query(default=5000, ge=1, le=100000),
    min_samples: int = Query(default=30, ge=1, le=5000),
    min_win_rate: float = Query(default=0.52, ge=0.0, le=1.0),
    min_profit_factor: float = Query(default=1.15, ge=0.0, le=1000.0),
    min_quality_score: float = Query(default=55.0, ge=0.0, le=100.0),
) -> dict[str, object]:
    return await darkflow_interaction_backtest(
        session,
        limit=limit,
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_quality_score=min_quality_score,
        persist=False,
    )


@router.post("/darkflow/interactions/backtest")
async def persist_darkflow_interaction_backtest_report(
    session: SessionDep,
    limit: int = Query(default=5000, ge=1, le=100000),
    min_samples: int = Query(default=30, ge=1, le=5000),
    min_win_rate: float = Query(default=0.52, ge=0.0, le=1.0),
    min_profit_factor: float = Query(default=1.15, ge=0.0, le=1000.0),
    min_quality_score: float = Query(default=55.0, ge=0.0, le=100.0),
) -> dict[str, object]:
    return await darkflow_interaction_backtest(
        session,
        limit=limit,
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_quality_score=min_quality_score,
        persist=True,
    )


@router.post("/darkflow/shadow-replay")
async def replay_darkflow_shadow_paper(
    session: SessionDep,
    limit: int = Query(default=500, ge=1, le=20000),
    min_profit_factor: float = Query(default=1.15, ge=0.0, le=1000.0),
    include_watchlist: bool = Query(default=True),
) -> dict[str, object]:
    return await darkflow_shadow_replay(
        session,
        limit=limit,
        min_profit_factor=min_profit_factor,
        include_watchlist=include_watchlist,
    )


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


@router.post("/features/research-reports/refresh")
async def refresh_research_reports(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
    min_samples: int = Query(default=30, ge=1, le=5000),
    limit: int = Query(default=DEFAULT_RESEARCH_QUERY_MAX_LIMIT, ge=1, le=100000),
    force: bool = True,
) -> dict[str, object]:
    requested_limit = int(limit)
    effective_limit = min(max(1, requested_limit), research_query_max_limit())
    active = await _active_research_report_task(session, horizon=horizon)
    if active is not None:
        return {
            "status": "already_running",
            "task_run_id": active.id,
            "task_status": active.status,
            "horizon": horizon,
            "min_samples": min_samples,
            "requested_limit": requested_limit,
            "limit": effective_limit,
            "limit_capped": requested_limit != effective_limit,
            "force": force,
        }
    result = await enqueue_task(
        session,
        task_name="features.research_reports",
        payload={"horizon": horizon, "min_samples": min_samples, "limit": requested_limit, "force": force},
    )
    return {
        **result,
        "horizon": horizon,
        "min_samples": min_samples,
        "requested_limit": requested_limit,
        "limit": effective_limit,
        "limit_capped": requested_limit != effective_limit,
        "force": force,
    }


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


@router.get("/features/candidates/latest")
async def latest_feature_candidates(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
) -> dict[str, object]:
    return await latest_feature_candidate_screen(session, horizon=horizon)


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


@router.get("/features/paper-ab/latest")
async def latest_feature_paper_ab_report(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
) -> dict[str, object]:
    return await latest_feature_paper_ab(session, horizon=horizon)


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


@router.get("/features/segment-candidates/latest")
async def latest_feature_segment_candidates(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
) -> dict[str, object]:
    return await latest_feature_segment_candidate_screen(session, horizon=horizon)


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


@router.get("/features/segment-paper-ab/latest")
async def latest_feature_segment_paper_ab_report(
    session: SessionDep,
    horizon: str = Query(default="30m", pattern="^(30m|1h|4h|24h)$"),
) -> dict[str, object]:
    return await latest_feature_segment_paper_ab(session, horizon=horizon)


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


async def _active_research_report_task(session: SessionDep, *, horizon: str) -> TaskRun | None:
    rows = await session.execute(
        select(TaskRun)
        .where(TaskRun.task_name == "features.research_reports", TaskRun.status.in_(_RESEARCH_REFRESH_ACTIVE_STATUSES))
        .order_by(TaskRun.queued_at.desc())
        .limit(20)
    )
    for item in rows.scalars():
        payload = item.payload or {}
        if str(payload.get("horizon") or "30m") == horizon and _is_active_research_task(item):
            return item
    return None


def _is_active_research_task(item: TaskRun) -> bool:
    now = datetime.now(timezone.utc)
    if item.status == "running":
        started_at = item.started_at or item.queued_at
        return _aware(started_at) >= now - timedelta(seconds=_RESEARCH_REFRESH_RUNNING_GRACE_SECONDS)
    if item.status in {"queued", "recorded"}:
        return _aware(item.queued_at) >= now - timedelta(seconds=_RESEARCH_REFRESH_QUEUED_GRACE_SECONDS)
    return False


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
