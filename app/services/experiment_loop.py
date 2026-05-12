from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.features import backfill_feature_events, backfill_feature_labels
from app.services.signal_attribution import backfill_signal_outcomes


DEFAULT_FEATURE_HORIZONS: tuple[str, ...] = ("30m", "1h", "4h", "24h")


async def run_experiment_backfill(
    session: AsyncSession,
    *,
    signal_limit: int = 500,
    feature_limit: int = 500,
    feature_label_limit: int | None = None,
    feature_horizons: Sequence[str] | None = None,
    include_feature_research: bool = True,
) -> dict[str, Any]:
    signal_result = await backfill_signal_outcomes(
        session,
        limit=signal_limit,
        commit=False,
    )
    payload: dict[str, Any] = {
        "signals": signal_result.__dict__,
        "features": {"enabled": False},
    }
    if include_feature_research:
        horizons = list(feature_horizons or DEFAULT_FEATURE_HORIZONS)
        event_result = await backfill_feature_events(
            session,
            limit=feature_limit,
            commit=False,
        )
        label_result = await backfill_feature_labels(
            session,
            limit=feature_label_limit or max(feature_limit * 10, feature_limit),
            horizons=horizons,
            commit=False,
        )
        payload["features"] = {
            "enabled": True,
            "horizons": horizons,
            "events": event_result.__dict__,
            "labels": label_result.__dict__,
        }
    await session.commit()
    return payload
