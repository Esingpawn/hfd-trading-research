from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    ASSETS,
    CORE_INDICATORS,
    EXPERIMENT_INDICATORS,
    HFD_INDICATORS,
    REQUIRED_SCORING_INDICATORS,
    TIMEFRAMES,
)
from app.models import BacktestRun, SignalObservation, SignalSnapshot
from app.models import FeatureEvent as FeatureEventModel, FeatureLabel


BACKTEST_WEIGHT_POLICY = {
    "used_for_execution_weights": False,
    "reason": "历史回测目前只作为策略初筛，不混入实时纸上交易权重。",
}

EXPERIMENT_POLICY = {
    "used_for_execution_weights": False,
    "used_for_opening_decisions": False,
    "minimum_labeled_samples_for_promotion": 100,
    "minimum_snapshot_count_for_feature_test": 27,
    "minimum_coverage_slots_for_feature_test": 27,
    "reason": "Experiment indicators are collected for feature validation only; they do not affect opening decisions or execution weights before promotion.",
}

SHARED_PAYLOAD_CANDIDATES = {
    "fair_value_gap",
    "cascade_liquidation_zones",
    "retail_stop_loss",
    "max_pain",
    "trend_roi",
    "time_exhaustion",
    "volume_exhaustion",
    "ob_decay",
    "liquidity_vacuum",
}


async def indicator_experiment_coverage(session: AsyncSession) -> dict[str, Any]:
    snapshot_counts = await _snapshot_counts(session)
    observation_counts = await _observation_counts(session)
    feature_counts = await _feature_counts(session)
    latest_backtest = await _latest_backtest(session)
    backtest_indicators = _backtest_indicator_keys(latest_backtest)
    catalog = [
        _catalog_row(
            key,
            snapshot_counts=snapshot_counts,
            observation_counts=observation_counts,
            feature_counts=feature_counts,
            backtest_indicators=backtest_indicators,
        )
        for key in HFD_INDICATORS
    ]
    return {
        "weight_sources": _weight_sources(observation_counts, latest_backtest),
        "experiment_policy": EXPERIMENT_POLICY,
        "experiment_matrix": _experiment_matrix(catalog),
        "indicator_catalog": catalog,
        "gaps": _gaps(catalog),
        "backtest": _backtest_payload(latest_backtest, backtest_indicators),
    }


async def _snapshot_counts(session: AsyncSession) -> dict[str, dict[str, Any]]:
    rows = await session.execute(
        select(
            SignalSnapshot.indicator,
            SignalSnapshot.symbol,
            SignalSnapshot.timeframe,
            func.count().label("count"),
            func.max(SignalSnapshot.created_at).label("latest_at"),
        ).group_by(
            SignalSnapshot.indicator,
            SignalSnapshot.symbol,
            SignalSnapshot.timeframe,
        )
    )
    stats: dict[str, dict[str, Any]] = {}
    for indicator, symbol, timeframe, count, latest_at in rows.all():
        key = str(indicator)
        bucket = stats.setdefault(
            key,
            {
                "snapshot_count": 0,
                "coverage_slots": 0,
                "latest_at": None,
                "symbols": set(),
                "timeframes": set(),
            },
        )
        bucket["snapshot_count"] += int(count)
        bucket["coverage_slots"] += 1
        bucket["symbols"].add(str(symbol))
        bucket["timeframes"].add(str(timeframe))
        if bucket["latest_at"] is None or latest_at > bucket["latest_at"]:
            bucket["latest_at"] = latest_at
    return {
        key: {
            **value,
            "symbols": sorted(value["symbols"]),
            "timeframes": sorted(value["timeframes"]),
        }
        for key, value in stats.items()
    }


async def _observation_counts(session: AsyncSession) -> dict[str, dict[str, int]]:
    rows = await session.execute(
        select(SignalObservation.signal_name, SignalObservation.labels)
    )
    counts: dict[str, dict[str, int]] = {}
    for raw_name, labels in rows.all():
        name = _fix_text(str(raw_name))
        bucket = counts.setdefault(name, {"total": 0, "labeled": 0, "pending": 0})
        bucket["total"] += 1
        if _has_horizon_label(labels, "4h"):
            bucket["labeled"] += 1
        else:
            bucket["pending"] += 1
    return counts


async def _feature_counts(session: AsyncSession) -> dict[str, dict[str, int]]:
    rows = await session.execute(
        select(
            FeatureEventModel.indicator,
            func.count(func.distinct(FeatureEventModel.id)).label("event_count"),
            func.count(func.distinct(FeatureLabel.id)).label("label_count"),
            func.count(func.distinct(case((FeatureLabel.status == "labeled", FeatureLabel.id), else_=None))).label(
                "labeled_count"
            ),
        )
        .outerjoin(FeatureLabel, FeatureLabel.feature_event_id == FeatureEventModel.id)
        .group_by(FeatureEventModel.indicator)
    )
    return {
        str(indicator): {
            "event_count": int(event_count or 0),
            "label_count": int(label_count or 0),
            "labeled_count": int(labeled_count or 0),
        }
        for indicator, event_count, label_count, labeled_count in rows.all()
    }


async def _latest_backtest(session: AsyncSession) -> BacktestRun | None:
    rows = await session.execute(
        select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(1)
    )
    return rows.scalar_one_or_none()


def _catalog_row(
    key: str,
    *,
    snapshot_counts: dict[str, dict[str, Any]],
    observation_counts: dict[str, dict[str, int]],
    feature_counts: dict[str, dict[str, int]],
    backtest_indicators: set[str],
) -> dict[str, Any]:
    item = HFD_INDICATORS[key]
    snapshot_payload = snapshot_counts.get(key, {})
    aliases = [_fix_text(alias) for alias in item.internal_aliases]
    live_counts = _sum_alias_counts(observation_counts, aliases)
    feature_payload = feature_counts.get(key, {})
    feature_event_count = int(feature_payload.get("event_count") or 0)
    feature_label_count = int(feature_payload.get("label_count") or 0)
    feature_labeled_count = int(feature_payload.get("labeled_count") or 0)
    collected = int(snapshot_payload.get("snapshot_count") or 0) > 0
    expected_slots = len(ASSETS) * len(TIMEFRAMES)
    coverage_slots = int(snapshot_payload.get("coverage_slots") or 0)
    return {
        **asdict(item),
        "included_in_core_collection": key in CORE_INDICATORS,
        "selected_for_experiment": key in EXPERIMENT_INDICATORS,
        "required_for_scoring": key in REQUIRED_SCORING_INDICATORS,
        "collected": collected,
        "snapshot_count": int(snapshot_payload.get("snapshot_count") or 0),
        "coverage_slots": coverage_slots,
        "expected_coverage_slots": expected_slots,
        "coverage_pct": round(coverage_slots / expected_slots, 4) if expected_slots else 0.0,
        "covered_symbols": snapshot_payload.get("symbols", []),
        "covered_timeframes": snapshot_payload.get("timeframes", []),
        "latest_snapshot_at": snapshot_payload.get("latest_at"),
        "used_in_live_strategy": bool(aliases),
        "live_observation_count": live_counts["total"],
        "live_labeled_count": live_counts["labeled"],
        "feature_event_count": feature_event_count,
        "feature_label_count": feature_label_count,
        "feature_labeled_count": feature_labeled_count,
        "research_sample_count": feature_labeled_count,
        "used_in_backtest": key in backtest_indicators,
        "used_for_execution_weights": False if key in EXPERIMENT_INDICATORS else bool(aliases),
        "used_for_opening_decisions": False if key in EXPERIMENT_INDICATORS else key in REQUIRED_SCORING_INDICATORS,
        "payload_status": _payload_status(key, collected),
        "evidence_level": _evidence_level(key, collected, live_counts, feature_labeled_count, backtest_indicators),
    }


def _sum_alias_counts(
    observation_counts: dict[str, dict[str, int]],
    aliases: list[str],
) -> dict[str, int]:
    total = labeled = pending = 0
    for alias in aliases:
        payload = observation_counts.get(alias) or {}
        total += int(payload.get("total") or 0)
        labeled += int(payload.get("labeled") or 0)
        pending += int(payload.get("pending") or 0)
    return {"total": total, "labeled": labeled, "pending": pending}


def _experiment_matrix(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = {row["key"]: row for row in catalog}
    return [_experiment_row(rows[key]) for key in EXPERIMENT_INDICATORS if key in rows]


def _experiment_row(row: dict[str, Any]) -> dict[str, Any]:
    status = _experiment_status(row)
    return {
        "key": row["key"],
        "hfd_name": row["hfd_name"],
        "english_name": row["english_name"],
        "family": row["family"],
        "role": row["role"],
        "collected": row["collected"],
        "snapshot_count": row["snapshot_count"],
        "coverage_slots": row["coverage_slots"],
        "expected_coverage_slots": row["expected_coverage_slots"],
        "coverage_pct": row["coverage_pct"],
        "covered_symbols": row["covered_symbols"],
        "covered_timeframes": row["covered_timeframes"],
        "latest_snapshot_at": row["latest_snapshot_at"],
        "live_observation_count": row["live_observation_count"],
        "live_labeled_count": row["live_labeled_count"],
        "feature_event_count": row["feature_event_count"],
        "feature_label_count": row["feature_label_count"],
        "feature_labeled_count": row["feature_labeled_count"],
        "research_sample_count": row["research_sample_count"],
        "used_for_execution_weights": False,
        "used_for_opening_decisions": False,
        "experiment_status": status,
        "recommended_next_action": _experiment_next_action(status),
        "evidence_level": row["evidence_level"],
    }


def _experiment_status(row: dict[str, Any]) -> str:
    if row["live_labeled_count"] >= EXPERIMENT_POLICY["minimum_labeled_samples_for_promotion"]:
        return "candidate_for_strategy"
    if row["coverage_slots"] >= EXPERIMENT_POLICY["minimum_coverage_slots_for_feature_test"]:
        return "ready_for_feature_test"
    if row["collected"]:
        return "collecting"
    return "not_collected"


def _experiment_next_action(status: str) -> str:
    return {
        "not_collected": "Collect experiment snapshots first across the default coins and three timeframes.",
        "collecting": "Keep collecting until each indicator has broad coin/timeframe coverage.",
        "ready_for_feature_test": "Run feature tests against win/loss, MFE/MAE, and direction confirmation.",
        "candidate_for_strategy": "Promote into candidate strategy features only after paper A/B validation.",
    }.get(status, "Keep observing.")


def _weight_sources(
    observation_counts: dict[str, dict[str, int]],
    latest_backtest: BacktestRun | None,
) -> list[dict[str, Any]]:
    live_total = sum(item["total"] for item in observation_counts.values())
    live_labeled = sum(item["labeled"] for item in observation_counts.values())
    backtest_results = latest_backtest.results if latest_backtest else []
    return [
        {
            "source": "paper_signal_attribution",
            "label": "实时/纸上归因",
            "sample_count": live_total,
            "labeled_count": live_labeled,
            "used_for_execution_weights": True,
            "status": "active",
            "reason": "当前权重治理只使用实时决策后的价格表现。",
        },
        {
            "source": "historical_backtest",
            "label": "历史回测初筛",
            "sample_count": sum(int(row.get("trade_count") or 0) for row in backtest_results),
            "labeled_count": len(backtest_results),
            "used_for_execution_weights": BACKTEST_WEIGHT_POLICY["used_for_execution_weights"],
            "status": "screening_only",
            "reason": BACKTEST_WEIGHT_POLICY["reason"],
        },
    ]


def _backtest_payload(
    latest_backtest: BacktestRun | None,
    backtest_indicators: set[str],
) -> dict[str, Any]:
    if latest_backtest is None:
        return {
            "strategy": None,
            "indicator_keys": [],
            "results_count": 0,
            "used_for_execution_weights": False,
        }
    return {
        "strategy": latest_backtest.strategy,
        "created_at": latest_backtest.created_at,
        "indicator_keys": sorted(backtest_indicators),
        "results_count": len(latest_backtest.results or []),
        "used_for_execution_weights": False,
        "params": latest_backtest.params,
    }


def _backtest_indicator_keys(latest_backtest: BacktestRun | None) -> set[str]:
    if latest_backtest is None:
        return set()
    if latest_backtest.strategy == "cost_band_retest_static_v0":
        return {"smart_money_cost"}
    return set()


def _gaps(catalog: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        "catalog_not_collected": [row["key"] for row in catalog if row["status"] == "catalog_only"],
        "experiment_not_collected": [
            row["key"]
            for row in catalog
            if row["selected_for_experiment"] and not row["collected"]
        ],
        "collected_not_weighted": [
            row["key"]
            for row in catalog
            if row["collected"] and not row["used_in_live_strategy"]
        ],
        "backtest_only_cost_band": [
            row["key"]
            for row in catalog
            if row["used_in_backtest"] and row["key"] == "smart_money_cost"
        ],
    }


def _payload_status(key: str, collected: bool) -> str:
    if collected:
        return "confirmed_collected"
    if key in SHARED_PAYLOAD_CANDIDATES:
        return "candidate_shared_payload"
    return "candidate_specific_payload"


def _evidence_level(
    key: str,
    collected: bool,
    live_counts: dict[str, int],
    feature_labeled_count: int,
    backtest_indicators: set[str],
) -> str:
    if live_counts["labeled"] >= 30:
        return "live_weight_ready"
    if live_counts["total"] > 0:
        return "live_observing"
    if feature_labeled_count >= EXPERIMENT_POLICY["minimum_labeled_samples_for_promotion"]:
        return "feature_research_ready"
    if feature_labeled_count > 0:
        return "feature_research_observing"
    if key in backtest_indicators:
        return "backtest_screened"
    if collected:
        return "collected_unmodeled"
    return "catalog_only"


def _fix_text(value: str) -> str:
    if not any("\u0080" <= ch <= "\u00ff" for ch in value):
        return value
    try:
        decoded = bytes(ord(ch) & 255 for ch in value).decode("utf-8", errors="replace")
    except ValueError:
        return value
    return decoded if any("\u4e00" <= ch <= "\u9fff" for ch in decoded) else value


def _has_horizon_label(labels: Any, horizon: str) -> bool:
    if not isinstance(labels, dict):
        return False
    return isinstance(labels.get(f"return_{horizon}"), (int, float))
