from datetime import datetime, timezone

from app.services.completeness import _as_aware, _is_stale


def test_as_aware_adds_utc_timezone() -> None:
    value = _as_aware(datetime(2026, 1, 1, 0, 0, 0))
    assert value.tzinfo is not None


def test_stale_detection_by_timeframe() -> None:
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
    old = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert _is_stale(old, now, "short")
    assert not _is_stale(old, now, "long")


def test_fresh_coverage_is_not_the_same_as_history_coverage() -> None:
    total_slots = 324
    present_slots = 324
    stale_slots = 189

    history_coverage = round(present_slots / total_slots, 4)
    fresh_coverage = round((present_slots - stale_slots) / total_slots, 4)

    assert history_coverage == 1.0
    assert fresh_coverage == 0.4167
