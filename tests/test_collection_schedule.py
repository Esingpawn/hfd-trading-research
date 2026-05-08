import pytest

from app.services.collection_schedule import research_due_timeframes, research_intervals


def test_research_due_timeframes_runs_missing_history_immediately() -> None:
    due = research_due_timeframes(
        ["short", "mid"],
        {},
        now=1000.0,
        intervals={"short": 1800, "mid": 3600, "long": 14400},
    )

    assert due == ["short", "mid"]


def test_research_due_timeframes_respects_per_timeframe_intervals() -> None:
    due = research_due_timeframes(
        ["short", "mid", "long"],
        {"short": 100.0, "mid": 100.0, "long": 100.0},
        now=2000.0,
        intervals={"short": 1800, "mid": 3600, "long": 14400},
    )

    assert due == ["short"]


def test_research_intervals_reject_non_positive_values() -> None:
    with pytest.raises(ValueError):
        research_intervals(short=0)
