from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.signal_attribution import signal_effectiveness


DEFAULT_HORIZON = "4h"
DEFAULT_MIN_SAMPLES = 30
MIN_MULTIPLIER = 0.65
MAX_MULTIPLIER = 1.25


async def signal_weight_governance(
    session: AsyncSession,
    *,
    horizon: str = DEFAULT_HORIZON,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict[str, Any]:
    report = await signal_effectiveness(session, min_samples=1, horizon=horizon)
    weights = [
        _weight_row(row, min_samples=min_samples)
        for row in report.get("signals", [])
    ]
    return {
        "horizon": horizon,
        "min_samples": min_samples,
        "sample_count": report.get("sample_count", 0),
        "labeled_count": report.get("labeled_count", 0),
        "pending_count": report.get("pending_count", 0),
        "weights": sorted(
            weights,
            key=lambda row: (row["status"] == "boost", row["multiplier"], row["sample_count"]),
            reverse=True,
        ),
    }


async def build_signal_weight_map(
    session: AsyncSession,
    *,
    horizon: str = DEFAULT_HORIZON,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict[str, dict[str, Any]]:
    governance = await signal_weight_governance(
        session,
        horizon=horizon,
        min_samples=min_samples,
    )
    return {
        row["signal_name"]: row
        for row in governance["weights"]
        if row["sample_count"] >= min_samples
    }


def _weight_row(row: dict[str, Any], *, min_samples: int) -> dict[str, Any]:
    sample_count = int(row.get("sample_count") or 0)
    win_rate = float(row.get("win_rate") or 0.0)
    avg_return = float(row.get("avg_return") or 0.0)
    profit_factor = row.get("profit_factor")
    if sample_count < min_samples:
        multiplier = 1.0
        status = "observing"
        reason = f"样本 {sample_count}/{min_samples}，只观察不调权。"
    else:
        multiplier = _multiplier(win_rate, avg_return, profit_factor)
        if multiplier >= 1.08:
            status = "boost"
            reason = "样本达标且收益、胜率或收益因子有正贡献，上调权重。"
        elif multiplier <= 0.92:
            status = "reduce"
            reason = "样本达标但统计表现偏弱，下调权重。"
        else:
            status = "neutral"
            reason = "样本达标但优势不明显，维持接近基础权重。"
    return {
        "signal_name": row.get("name"),
        "role": row.get("role"),
        "sample_count": sample_count,
        "win_rate": win_rate,
        "avg_return": avg_return,
        "profit_factor": profit_factor,
        "multiplier": multiplier,
        "status": status,
        "reason": reason,
    }


def _multiplier(win_rate: float, avg_return: float, profit_factor: Any) -> float:
    return_component = _clamp(avg_return / 0.02, -0.12, 0.12)
    win_component = _clamp((win_rate - 0.5) * 0.8, -0.12, 0.12)
    pf_component = 0.0
    if isinstance(profit_factor, (int, float)):
        capped_pf = min(max(float(profit_factor), 0.0), 2.0)
        pf_component = _clamp((capped_pf - 1.0) * 0.08, -0.08, 0.08)
    return round(_clamp(1.0 + return_component + win_component + pf_component, MIN_MULTIPLIER, MAX_MULTIPLIER), 3)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
