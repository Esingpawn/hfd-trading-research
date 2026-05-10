from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.services.experiment_effectiveness import experiment_feature_effectiveness
from app.services.features import (
    backfill_feature_events,
    backfill_feature_labels,
    feature_effectiveness,
    refresh_feature_research,
)
from app.services.indicator_catalog import indicator_experiment_coverage
from app.services.signal_attribution import backfill_signal_outcomes, signal_effectiveness
from app.services.signal_weights import signal_weight_governance

router = APIRouter()


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
) -> dict[str, object]:
    result = await backfill_feature_labels(session, limit=limit, horizons=horizons)
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
