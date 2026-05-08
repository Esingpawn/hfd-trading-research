from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import ASSETS, CORE_INDICATORS, REQUIRED_SCORING_INDICATORS, TIMEFRAMES
from app.models import SignalSnapshot


STALE_MINUTES_BY_TIMEFRAME = {
    "short": 45,
    "mid": 90,
    "long": 360,
}


async def data_completeness(session: AsyncSession) -> dict[str, Any]:
    ranked = (
        select(
            SignalSnapshot.id.label("id"),
            func.row_number()
            .over(
                partition_by=(
                    SignalSnapshot.symbol,
                    SignalSnapshot.timeframe,
                    SignalSnapshot.indicator,
                ),
                order_by=SignalSnapshot.created_at.desc(),
            )
            .label("rn"),
        )
        .subquery()
    )
    rows = await session.execute(
        select(
            SignalSnapshot.symbol,
            SignalSnapshot.timeframe,
            SignalSnapshot.indicator,
            SignalSnapshot.collected_at,
        )
        .join(ranked, SignalSnapshot.id == ranked.c.id)
        .where(ranked.c.rn == 1)
    )
    latest: dict[tuple[str, str, str], datetime] = {}
    for symbol, timeframe, indicator, collected_at in rows.all():
        latest[(symbol.replace("USDT", ""), timeframe, indicator)] = collected_at

    matrix: list[dict[str, Any]] = []
    total_slots = len(ASSETS) * len(TIMEFRAMES) * len(CORE_INDICATORS)
    scoring_total_slots = len(ASSETS) * len(TIMEFRAMES) * len(REQUIRED_SCORING_INDICATORS)
    present_slots = 0
    scoring_present_slots = 0
    stale_slots = 0
    scoring_stale_slots = 0
    now = datetime.now(timezone.utc)

    for coin, asset in ASSETS.items():
        row: dict[str, Any] = {
            "coin": coin,
            "symbol": f"{coin}USDT",
            "tier": asset.tier,
            "timeframes": {},
            "ready": True,
            "scoring_ready": True,
            "missing_count": 0,
            "scoring_missing_count": 0,
            "stale_count": 0,
            "scoring_stale_count": 0,
            "coverage_pct": 0.0,
            "scoring_coverage_pct": 0.0,
            "fresh_count": 0,
            "scoring_fresh_count": 0,
            "fresh_coverage_pct": 0.0,
            "scoring_fresh_coverage_pct": 0.0,
        }
        present_for_coin = 0
        scoring_present_for_coin = 0
        stale_for_coin = 0
        scoring_stale_for_coin = 0
        for timeframe in TIMEFRAMES:
            tf_payload = {
                "status": "ok",
                "scoring_status": "ok",
                "latest_at": None,
                "present": [],
                "scoring_present": [],
                "missing": [],
                "scoring_missing": [],
                "stale": [],
                "scoring_stale": [],
            }
            for indicator in CORE_INDICATORS:
                is_required = indicator in REQUIRED_SCORING_INDICATORS
                item_time_raw = latest.get((coin, timeframe, indicator))
                if item_time_raw is None:
                    tf_payload["missing"].append(indicator)
                    row["missing_count"] += 1
                    if is_required:
                        tf_payload["scoring_missing"].append(indicator)
                        row["scoring_missing_count"] += 1
                    continue
                present_slots += 1
                present_for_coin += 1
                tf_payload["present"].append(indicator)
                if is_required:
                    scoring_present_slots += 1
                    scoring_present_for_coin += 1
                    tf_payload["scoring_present"].append(indicator)
                item_time = _as_aware(item_time_raw)
                latest_at = tf_payload["latest_at"]
                if latest_at is None or item_time > latest_at:
                    tf_payload["latest_at"] = item_time
                if _is_stale(item_time, now, timeframe):
                    tf_payload["stale"].append(indicator)
                    row["stale_count"] += 1
                    stale_for_coin += 1
                    stale_slots += 1
                    if is_required:
                        tf_payload["scoring_stale"].append(indicator)
                        row["scoring_stale_count"] += 1
                        scoring_stale_for_coin += 1
                        scoring_stale_slots += 1
            if tf_payload["missing"]:
                tf_payload["status"] = "missing"
                row["ready"] = False
            if tf_payload["stale"]:
                tf_payload["status"] = "stale"
                row["ready"] = False
            if tf_payload["scoring_missing"]:
                tf_payload["scoring_status"] = "missing"
                row["scoring_ready"] = False
            if tf_payload["scoring_stale"]:
                tf_payload["scoring_status"] = "stale"
                row["scoring_ready"] = False
            tf_payload["latest_at"] = (
                tf_payload["latest_at"].isoformat() if tf_payload["latest_at"] else None
            )
            row["timeframes"][timeframe] = tf_payload
        row["coverage_pct"] = round(
            present_for_coin / (len(TIMEFRAMES) * len(CORE_INDICATORS)), 4
        )
        row["scoring_coverage_pct"] = round(
            scoring_present_for_coin
            / (len(TIMEFRAMES) * len(REQUIRED_SCORING_INDICATORS)),
            4,
        )
        row["fresh_count"] = present_for_coin - stale_for_coin
        row["scoring_fresh_count"] = scoring_present_for_coin - scoring_stale_for_coin
        row["fresh_coverage_pct"] = round(
            row["fresh_count"] / (len(TIMEFRAMES) * len(CORE_INDICATORS)), 4
        )
        row["scoring_fresh_coverage_pct"] = round(
            row["scoring_fresh_count"]
            / (len(TIMEFRAMES) * len(REQUIRED_SCORING_INDICATORS)),
            4,
        )
        matrix.append(row)

    fresh_slots = present_slots - stale_slots
    scoring_fresh_slots = scoring_present_slots - scoring_stale_slots
    research_summary = {
        "total_slots": total_slots,
        "present_slots": present_slots,
        "fresh_slots": fresh_slots,
        "missing_slots": total_slots - present_slots,
        "stale_slots": stale_slots,
        "coverage_pct": round(present_slots / total_slots, 4) if total_slots else 0,
        "fresh_coverage_pct": round(fresh_slots / total_slots, 4) if total_slots else 0,
    }
    scoring_summary = {
        "total_slots": scoring_total_slots,
        "present_slots": scoring_present_slots,
        "fresh_slots": scoring_fresh_slots,
        "missing_slots": scoring_total_slots - scoring_present_slots,
        "stale_slots": scoring_stale_slots,
        "coverage_pct": (
            round(scoring_present_slots / scoring_total_slots, 4)
            if scoring_total_slots
            else 0
        ),
        "fresh_coverage_pct": (
            round(scoring_fresh_slots / scoring_total_slots, 4)
            if scoring_total_slots
            else 0
        ),
    }
    return {
        "summary": {
            **research_summary,
            "research": research_summary,
            "scoring": scoring_summary,
            "scoring_total_slots": scoring_summary["total_slots"],
            "scoring_present_slots": scoring_summary["present_slots"],
            "scoring_fresh_slots": scoring_summary["fresh_slots"],
            "scoring_missing_slots": scoring_summary["missing_slots"],
            "scoring_stale_slots": scoring_summary["stale_slots"],
            "scoring_coverage_pct": scoring_summary["coverage_pct"],
            "scoring_fresh_coverage_pct": scoring_summary["fresh_coverage_pct"],
        },
        "matrix": matrix,
    }


def _is_stale(value: datetime, now: datetime, timeframe: str) -> bool:
    age_seconds = (now - value).total_seconds()
    return age_seconds > STALE_MINUTES_BY_TIMEFRAME[timeframe] * 60


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
