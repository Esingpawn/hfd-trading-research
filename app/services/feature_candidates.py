from __future__ import annotations

from collections import Counter
from statistics import mean
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExperimentRun, FeatureEvent as FeatureEventModel, FeatureLabel
from app.services.features import FEATURE_HORIZONS


DEFAULT_MIN_SAMPLES = 30
DEFAULT_MIN_WIN_RATE = 0.52
DEFAULT_MIN_PROFIT_FACTOR = 1.2
DEFAULT_MIN_AVG_RETURN = 0.0
DEFAULT_SEGMENT_MIN_SAMPLES = 5
DEFAULT_MIN_SEGMENTS = 2


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
    candidate_pairs = [(event, label) for event, label in pairs if _feature_key(event) in selected_keys]
    control_pairs = [(event, label) for event, label in pairs if _feature_key(event) not in selected_keys]
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
                    [(event, label) for event, label in candidate_pairs if _feature_key(event) == row["feature_key"]]
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
        limit=limit,
        persist=False,
    )
    selected = segment_report["candidates"][:candidate_limit]
    selected_keys = {str(row["segment_key"]) for row in selected}
    selected_symbol_timeframes = {str(row["symbol_timeframe"]) for row in selected}
    pairs = await _labeled_feature_pairs(session, horizon=horizon, limit=limit)
    candidate_pairs = [(event, label) for event, label in pairs if _segment_key(event) in selected_keys]
    matched_control_pairs = [
        (event, label)
        for event, label in pairs
        if _symbol_timeframe(event) in selected_symbol_timeframes and _segment_key(event) not in selected_keys
    ]
    all_control_pairs = [(event, label) for event, label in pairs if _segment_key(event) not in selected_keys]
    candidate_stats = _pseudo_trade_stats(candidate_pairs)
    matched_control_stats = _pseudo_trade_stats(matched_control_pairs)
    all_control_stats = _pseudo_trade_stats(all_control_pairs)
    report: dict[str, Any] = {
        "horizon": horizon,
        "limit": limit,
        "candidate_limit": candidate_limit,
        "selected_candidate_count": len(selected),
        "selected_segment_keys": [row["segment_key"] for row in selected],
        "thresholds": segment_report["thresholds"],
        "policy": _research_policy(),
        "data_quality": {
            "labeled_count": len(pairs),
            "candidate_pseudo_trade_count": candidate_stats["trade_count"],
            "matched_control_pseudo_trade_count": matched_control_stats["trade_count"],
            "all_control_pseudo_trade_count": all_control_stats["trade_count"],
            "status": "ready" if selected else "no_segment_candidate_features",
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
                    [(event, label) for event, label in candidate_pairs if _segment_key(event) == row["segment_key"]]
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
            params=segment_report["thresholds"],
            metrics=report,
            notes="Report-only segment-aware paper A/B using feature labels as pseudo-trades; no PaperTrade rows are opened.",
        )
    return report


async def _labeled_feature_pairs(
    session: AsyncSession,
    *,
    horizon: str,
    limit: int,
) -> list[tuple[FeatureEventModel, FeatureLabel]]:
    rows = await session.execute(
        select(FeatureEventModel, FeatureLabel)
        .where(
            FeatureLabel.feature_event_id == FeatureEventModel.id,
            FeatureLabel.horizon == horizon,
            FeatureLabel.status == "labeled",
        )
        .order_by(FeatureEventModel.event_ts.desc())
        .limit(limit)
    )
    return [
        (event, label)
        for event, label in rows.all()
        if isinstance(label.return_pct, (int, float)) and event.direction in {"long", "short"}
    ]


def _candidate_rows(
    pairs: list[tuple[FeatureEventModel, FeatureLabel]],
    *,
    thresholds: dict[str, Any],
) -> list[dict[str, Any]]:
    buckets: dict[str, list[tuple[FeatureEventModel, FeatureLabel]]] = {}
    for event, label in pairs:
        buckets.setdefault(_feature_key(event), []).append((event, label))
    rows = []
    for feature_key, items in buckets.items():
        stats = _pseudo_trade_stats(items)
        first = items[0][0]
        segment_report = _segment_report(items, thresholds=thresholds)
        reasons = _candidate_reasons(stats, segment_report, thresholds=thresholds)
        promotion_status = _promotion_status(reasons)
        symbols = sorted({event.symbol for event, _label in items})
        timeframes = sorted({event.timeframe for event, _label in items})
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
    pairs: list[tuple[FeatureEventModel, FeatureLabel]],
    *,
    thresholds: dict[str, Any],
) -> list[dict[str, Any]]:
    buckets: dict[str, list[tuple[FeatureEventModel, FeatureLabel]]] = {}
    for event, label in pairs:
        buckets.setdefault(_segment_key(event), []).append((event, label))
    rows = []
    for segment_key, items in buckets.items():
        stats = _pseudo_trade_stats(items)
        first = items[0][0]
        reasons = _segment_candidate_reasons(stats, thresholds=thresholds)
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
                "win_rate": stats["win_rate"],
                "avg_return": stats["avg_return"],
                "median_return": stats["median_return"],
                "profit_factor": stats["profit_factor"],
                "avg_mfe": stats["avg_mfe"],
                "avg_mae": stats["avg_mae"],
                "avg_strength": _avg_strength(items),
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
    items: list[tuple[FeatureEventModel, FeatureLabel]],
    *,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    buckets: dict[str, list[tuple[FeatureEventModel, FeatureLabel]]] = {}
    for event, label in items:
        buckets.setdefault(f"{event.symbol}:{event.timeframe}", []).append((event, label))
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


def _segment_candidate_reasons(stats: dict[str, Any], *, thresholds: dict[str, Any]) -> list[str]:
    reasons = []
    if stats["trade_count"] < thresholds["min_samples"]:
        reasons.append("sample_count_below_minimum")
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


def _pseudo_trade_stats(items: list[tuple[FeatureEventModel, FeatureLabel]]) -> dict[str, Any]:
    values = [float(label.return_pct) for _event, label in items if isinstance(label.return_pct, (int, float))]
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
) -> dict[str, Any]:
    return {
        "min_samples": int(min_samples),
        "min_win_rate": float(min_win_rate),
        "min_profit_factor": float(min_profit_factor),
        "min_avg_return": float(min_avg_return),
        "segment_min_samples": int(segment_min_samples),
        "min_segments": int(min_segments),
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


def _segment_candidate_feature_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row["feature_key"]), []).append(row)
    summary = []
    for feature_key, items in buckets.items():
        total_samples = sum(int(row["sample_count"] or 0) for row in items)
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
                "weighted_avg_return": weighted_return,
                "weighted_win_rate": weighted_win_rate,
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


def _avg_strength(items: list[tuple[FeatureEventModel, FeatureLabel]]) -> float | None:
    values = [float(event.strength) for event, _label in items if isinstance(event.strength, (int, float))]
    return mean(values) if values else None


def _avg_label(items: list[tuple[FeatureEventModel, FeatureLabel]], field: str) -> float | None:
    values = [getattr(label, field) for _event, label in items]
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
