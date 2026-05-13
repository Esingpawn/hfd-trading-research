from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FeatureEvent, FeatureLabel, PriceSnapshot, SignalSnapshot
from app.services.completeness import data_completeness


async def data_quality_report(session: AsyncSession) -> dict[str, Any]:
    completeness = await data_completeness(session)
    price_summary = await _price_summary(session)
    feature_summary = await _feature_summary(session)
    issues = _quality_issues(completeness, price_summary, feature_summary)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": _status(issues),
        "issues": issues,
        "completeness": completeness["summary"],
        "prices": price_summary,
        "features": feature_summary,
        "policy": {
            "read_only": True,
            "opens_paper_trades": False,
            "opens_live_orders": False,
        },
    }


async def _price_summary(session: AsyncSession) -> dict[str, Any]:
    total = int((await session.execute(select(func.count()).select_from(PriceSnapshot))).scalar_one())
    invalid = int(
        (
            await session.execute(
                select(func.count()).select_from(PriceSnapshot).where(PriceSnapshot.price <= 0)
            )
        ).scalar_one()
    )
    symbol_rows = await session.execute(
        select(PriceSnapshot.symbol, func.count().label("count"))
        .group_by(PriceSnapshot.symbol)
        .order_by(func.count().desc())
    )
    latest = await session.execute(select(func.max(PriceSnapshot.created_at)))
    return {
        "total": total,
        "invalid_price_count": invalid,
        "symbol_count": len(symbol_rows.all()),
        "latest_created_at": _iso(latest.scalar_one_or_none()),
    }


async def _feature_summary(session: AsyncSession) -> dict[str, Any]:
    event_count = int((await session.execute(select(func.count()).select_from(FeatureEvent))).scalar_one())
    label_count = int((await session.execute(select(func.count()).select_from(FeatureLabel))).scalar_one())
    labeled_count = int(
        (
            await session.execute(
                select(func.count()).select_from(FeatureLabel).where(FeatureLabel.status == "labeled")
            )
        ).scalar_one()
    )
    pending_count = int(
        (
            await session.execute(
                select(func.count()).select_from(FeatureLabel).where(FeatureLabel.status == "pending")
            )
        ).scalar_one()
    )
    return {
        "event_count": event_count,
        "label_count": label_count,
        "labeled_count": labeled_count,
        "pending_count": pending_count,
        "label_per_event_ratio": round(label_count / event_count, 4) if event_count else 0.0,
        "labeled_label_ratio": round(labeled_count / label_count, 4) if label_count else 0.0,
    }


def _quality_issues(
    completeness: dict[str, Any],
    prices: dict[str, Any],
    features: dict[str, Any],
) -> list[dict[str, Any]]:
    summary = completeness.get("summary") or {}
    scoring = summary.get("scoring") or {}
    research = summary.get("research") or {}
    issues: list[dict[str, Any]] = []
    if int(scoring.get("missing_slots") or 0) or int(scoring.get("stale_slots") or 0):
        issues.append(
            {
                "code": "scoring_not_fresh",
                "severity": "error",
                "message": "Scoring data is missing or stale; opening confidence should be downgraded.",
                "metrics": {
                    "missing_slots": int(scoring.get("missing_slots") or 0),
                    "stale_slots": int(scoring.get("stale_slots") or 0),
                },
            }
        )
    if int(research.get("missing_slots") or 0) or int(research.get("stale_slots") or 0):
        issues.append(
            {
                "code": "research_not_fresh",
                "severity": "warning",
                "message": "Research coverage is incomplete; feature reports may be less representative.",
                "metrics": {
                    "missing_slots": int(research.get("missing_slots") or 0),
                    "stale_slots": int(research.get("stale_slots") or 0),
                },
            }
        )
    if int(prices.get("invalid_price_count") or 0):
        issues.append(
            {
                "code": "invalid_prices",
                "severity": "error",
                "message": "Non-positive price snapshots exist and should be investigated.",
                "metrics": {"invalid_price_count": int(prices.get("invalid_price_count") or 0)},
            }
        )
    if features.get("event_count") and not features.get("label_count"):
        issues.append(
            {
                "code": "feature_labels_missing",
                "severity": "warning",
                "message": "Feature events exist but labels have not started accumulating.",
                "metrics": {"event_count": features.get("event_count"), "label_count": features.get("label_count")},
            }
        )
    return issues


def _status(issues: list[dict[str, Any]]) -> str:
    if any(issue.get("severity") == "error" for issue in issues):
        return "error"
    if issues:
        return "warning"
    return "ok"


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None
