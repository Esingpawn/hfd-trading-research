from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.feature_candidates import generate_default_research_reports
from app.services.features import FeatureLabelBackfillResult, backfill_feature_events, backfill_feature_labels
from app.services.shadow_paper import mark_shadow_paper_trades, shadow_paper_scan
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
    include_research_reports: bool = False,
    research_report_horizon: str = "30m",
    research_report_min_samples: int = 30,
    research_report_limit: int = 5000,
    research_report_max_age_seconds: int | None = None,
    include_shadow_paper: bool = False,
    shadow_candidate_limit: int = 20,
    shadow_include_watchlist: bool = True,
) -> dict[str, Any]:
    signal_result = await backfill_signal_outcomes(
        session,
        limit=signal_limit,
        commit=True,
    )
    payload: dict[str, Any] = {
        "signals": signal_result.__dict__,
        "features": {"enabled": False},
        "research_reports": {"enabled": False},
        "shadow_paper": {"enabled": False},
    }
    if include_feature_research:
        horizons = list(feature_horizons or DEFAULT_FEATURE_HORIZONS)
        event_result = await backfill_feature_events(
            session,
            limit=feature_limit,
            commit=True,
        )
        label_limit = feature_label_limit or max(feature_limit * 10, feature_limit)
        label_result = await _backfill_feature_labels_incremental(
            session,
            limit=label_limit,
            horizons=horizons,
        )
        payload["features"] = {
            "enabled": True,
            "horizons": horizons,
            "commit_strategy": "stage_commits_by_signal_events_and_horizon",
            "events": event_result.__dict__,
            "labels": label_result.__dict__,
        }
    if include_research_reports:
        payload["research_reports"] = await generate_default_research_reports(
            session,
            horizon=research_report_horizon,
            min_samples=research_report_min_samples,
            limit=research_report_limit,
            max_age_seconds=research_report_max_age_seconds,
        )
    if include_shadow_paper:
        payload["shadow_paper"] = {
            "enabled": True,
            "mark": await mark_shadow_paper_trades(session),
            "scan": await shadow_paper_scan(
                session,
                candidate_limit=shadow_candidate_limit,
                include_watchlist=shadow_include_watchlist,
            ),
        }
    return payload


async def _backfill_feature_labels_incremental(
    session: AsyncSession,
    *,
    limit: int,
    horizons: Sequence[str],
) -> FeatureLabelBackfillResult:
    horizon_limits = _split_limit(limit, len(horizons))
    per_horizon: dict[str, dict[str, int]] = {}
    total = FeatureLabelBackfillResult(0, 0, 0, 0, 0)
    for horizon, horizon_limit in zip(horizons, horizon_limits, strict=False):
        result = await backfill_feature_labels(
            session,
            limit=horizon_limit,
            horizons=[horizon],
            commit=True,
        )
        per_horizon[horizon] = _label_result_counts(result)
        total = _sum_label_results(total, result)
    return FeatureLabelBackfillResult(
        events_scanned=total.events_scanned,
        labels_labeled=total.labels_labeled,
        labels_pending=total.labels_pending,
        labels_skipped=total.labels_skipped,
        labels_refreshed=total.labels_refreshed,
        horizon_results=per_horizon,
    )


def _split_limit(limit: int, parts: int) -> list[int]:
    if parts <= 0:
        return []
    safe_limit = max(0, int(limit))
    base = safe_limit // parts
    remainder = safe_limit % parts
    return [base + (1 if index < remainder else 0) for index in range(parts)]


def _label_result_counts(result: Any) -> dict[str, int]:
    return {
        "events_scanned": int(result.events_scanned),
        "labels_labeled": int(result.labels_labeled),
        "labels_pending": int(result.labels_pending),
        "labels_skipped": int(result.labels_skipped),
        "labels_refreshed": int(result.labels_refreshed),
    }


def _sum_label_results(left: FeatureLabelBackfillResult, right: FeatureLabelBackfillResult) -> FeatureLabelBackfillResult:
    return FeatureLabelBackfillResult(
        events_scanned=left.events_scanned + right.events_scanned,
        labels_labeled=left.labels_labeled + right.labels_labeled,
        labels_pending=left.labels_pending + right.labels_pending,
        labels_skipped=left.labels_skipped + right.labels_skipped,
        labels_refreshed=left.labels_refreshed + right.labels_refreshed,
        horizon_results=None,
    )
