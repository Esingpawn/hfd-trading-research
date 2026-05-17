from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.domain.darkflow.interactions import (
    interaction_key,
    normalize_klines,
    playbook_blockers,
    playbook_display_name,
    playbook_for_zone,
    quality_grade,
    zone_key,
)


def test_normalize_klines_sorts_and_repairs_high_low() -> None:
    rows = normalize_klines([
        {"timestamp": "2026-01-01T00:01:00+00:00", "open": 100, "high": 99, "low": 98, "close": 101},
        [datetime(2026, 1, 1, tzinfo=timezone.utc), 100, 99, 98, 101],
        {"timestamp": "bad", "open": 100, "high": 101, "low": 99, "close": 100},
    ])

    assert [item.ts.isoformat() for item in rows] == [
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:01:00+00:00",
    ]
    assert rows[0].high == 101.0
    assert rows[0].low == 98.0
    assert rows[1].high == 101.0


def test_playbook_mapping_uses_family_and_playbook_fallback() -> None:
    playbooks = [
        SimpleNamespace(key="custom_entry", display_name="Custom Entry", entry_indicators=("custom_indicator",), blocker_indicators=("blocked_by",)),
    ]

    assert playbook_for_zone({"indicator": "liq_heatmap", "family": "liquidity"}, "wick_pierce_reclaim", playbooks=playbooks) == "liquidity_sweep_reversal"
    assert playbook_for_zone({"indicator": "smart_money_cost", "family": "cost_structure"}, "first_touch", playbooks=playbooks) == "pullback_to_cost"
    assert playbook_for_zone({"indicator": "custom_indicator", "family": "unknown"}, "first_touch", playbooks=playbooks) == "custom_entry"
    assert playbook_display_name("custom_entry", playbooks=playbooks) == "Custom Entry"
    assert playbook_blockers("custom_entry", playbooks=playbooks) == ("blocked_by",)


def test_quality_grade_and_stable_keys() -> None:
    event_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert quality_grade(80) == "A"
    assert quality_grade(60) == "B"
    assert quality_grade(45) == "C"
    assert quality_grade(44.99) == "D"
    assert zone_key(a=1, b={"ts": event_ts}) == zone_key(b={"ts": event_ts}, a=1)
    assert interaction_key("zone", "first_touch", event_ts, schema="v2") == interaction_key("zone", "first_touch", event_ts, schema="v2")
