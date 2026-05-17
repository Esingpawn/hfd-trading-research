from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.trade_candidates.entry_plan import (
    FROZEN_ENTRY_PLAN_TYPE,
    build_frozen_entry_plan,
    candidate_plan_openable,
    entry_plan_state,
    missing_price_entry_plan_state,
)


def test_build_frozen_entry_plan_uses_source_zone_and_validity_floor() -> None:
    event_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    plan = build_frozen_entry_plan(
        direction="long",
        interaction_type="first_touch",
        interval="5m",
        event_ts=event_ts,
        context={"zone": {"lower_price": 99.5, "upper_price": 100.5}, "hold_bars": 4},
        entry=100.0,
        stop=99.0,
        target=102.0,
        invalidation_price=None,
    )

    assert plan["plan_type"] == FROZEN_ENTRY_PLAN_TYPE
    assert plan["trigger"] == "first_touch_zone_reaction"
    assert plan["entry_range"] == {"lower": 99.5, "upper": 100.5, "source": "source_darkflow_zone"}
    assert plan["valid_until"] == "2026-01-01T02:00:00+00:00"


def test_entry_plan_state_waiting_triggered_missed_and_invalidated() -> None:
    now = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    plan = {
        "plan_type": FROZEN_ENTRY_PLAN_TYPE,
        "planned_entry": 100.0,
        "planned_stop": 99.0,
        "invalidation_price": 99.0,
        "take_profit_levels": [{"price": 102.0}],
        "entry_range": {"lower": 99.5, "upper": 100.5, "source": "source_darkflow_zone"},
        "valid_until": (now + timedelta(hours=1)).isoformat(),
    }

    waiting = entry_plan_state(
        plan=plan,
        direction="long",
        fallback_entry=100.0,
        fallback_stop=99.0,
        fallback_target=102.0,
        mark_price=99.2,
        now=now,
        entry_tolerance_pct=0.05,
    )
    triggered = entry_plan_state(
        plan=plan,
        direction="long",
        fallback_entry=100.0,
        fallback_stop=99.0,
        fallback_target=102.0,
        mark_price=100.0,
        now=now,
        entry_tolerance_pct=0.05,
    )
    missed = entry_plan_state(
        plan=plan,
        direction="long",
        fallback_entry=100.0,
        fallback_stop=99.0,
        fallback_target=102.0,
        mark_price=101.0,
        now=now,
        entry_tolerance_pct=0.05,
    )
    invalidated = entry_plan_state(
        plan=plan,
        direction="long",
        fallback_entry=100.0,
        fallback_stop=99.0,
        fallback_target=102.0,
        mark_price=98.9,
        now=now,
        entry_tolerance_pct=0.05,
    )

    assert waiting["state"] == "waiting"
    assert triggered["state"] == "triggered"
    assert missed["state"] == "missed"
    assert invalidated["state"] == "invalidated"


def test_entry_plan_state_expired_and_missing_price() -> None:
    now = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    expired_plan = {
        "planned_entry": 100.0,
        "planned_stop": 99.0,
        "take_profit_levels": [{"price": 102.0}],
        "entry_range": {"lower": 99.5, "upper": 100.5},
        "valid_until": (now - timedelta(minutes=1)).isoformat(),
    }

    expired = entry_plan_state(
        plan=expired_plan,
        direction="long",
        fallback_entry=100.0,
        fallback_stop=99.0,
        fallback_target=102.0,
        mark_price=100.0,
        now=now,
        entry_tolerance_pct=0.05,
    )
    missing = missing_price_entry_plan_state(
        plan=expired_plan,
        direction="long",
        fallback_entry=100.0,
        fallback_stop=99.0,
        fallback_target=102.0,
        now=now,
        entry_tolerance_pct=0.05,
    )

    assert expired["state"] == "expired"
    assert missing["state"] == "expired"
    assert missing["mark_price"] is None


def test_candidate_plan_openable_preserves_shape_checks() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert candidate_plan_openable(
        direction="long",
        entry_price=100.0,
        stop_price=99.0,
        target_price=102.0,
        setup_time=now,
        now=now,
        max_candidate_age_hours=72,
    ) is None
    assert candidate_plan_openable(
        direction="long",
        entry_price=100.0,
        stop_price=101.0,
        target_price=102.0,
        setup_time=now,
        now=now,
        max_candidate_age_hours=72,
    ) == "invalid_long_reward_shape"
    assert candidate_plan_openable(
        direction="long",
        entry_price=100.0,
        stop_price=99.0,
        target_price=102.0,
        setup_time=now - timedelta(hours=73),
        now=now,
        max_candidate_age_hours=72,
    ) == "stale_candidate"
