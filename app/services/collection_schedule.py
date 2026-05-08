from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.constants import TIMEFRAMES


DEFAULT_RESEARCH_INTERVAL_SECONDS = {
    "short": 1800,
    "mid": 3600,
    "long": 14400,
}


def research_due_timeframes(
    selected_timeframes: Sequence[str],
    last_completed_at: Mapping[str, float],
    now: float,
    intervals: Mapping[str, int],
) -> list[str]:
    due: list[str] = []
    for timeframe in selected_timeframes:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unknown timeframe: {timeframe}")
        interval = intervals[timeframe]
        last_completed = last_completed_at.get(timeframe)
        if last_completed is None or now - last_completed >= interval:
            due.append(timeframe)
    return due


def research_intervals(
    *,
    short: int = DEFAULT_RESEARCH_INTERVAL_SECONDS["short"],
    mid: int = DEFAULT_RESEARCH_INTERVAL_SECONDS["mid"],
    long: int = DEFAULT_RESEARCH_INTERVAL_SECONDS["long"],
) -> dict[str, int]:
    values = {"short": short, "mid": mid, "long": long}
    for timeframe, value in values.items():
        if value <= 0:
            raise ValueError(f"{timeframe} research interval must be positive")
    return values
