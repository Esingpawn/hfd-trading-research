import pytest

from app.services.signal_weights import _multiplier, _weight_row
from app.services.strategy import _score_states, TimeframeState


def test_weight_row_keeps_small_samples_observing() -> None:
    row = _weight_row(
        {"name": "长期方向", "role": "direction", "sample_count": 12, "win_rate": 0.8, "avg_return": 0.05},
        min_samples=30,
    )

    assert row["multiplier"] == 1.0
    assert row["status"] == "observing"


def test_multiplier_is_bounded_for_strong_and_weak_signals() -> None:
    assert _multiplier(0.95, 0.20, 10.0) == 1.25
    weak = _multiplier(0.05, -0.20, 0.1)
    assert weak >= 0.65
    assert weak < 1.0


def test_score_breakdown_includes_governed_weighted_score_without_changing_decision() -> None:
    result = _score_states(
        "BTCUSDT",
        "core",
        103.0,
        [
            TimeframeState("short", "30m", "long", 100.0, 0.03, "Ongoing", "s1"),
            TimeframeState("mid", "1h", "long", 100.0, 0.03, "Ongoing", "s2"),
            TimeframeState("long", "4h", "long", 100.0, 0.03, "Ongoing", "s3"),
        ],
        {
            "smart_money_cost": object(),
            "liq_heatmap": object(),
            "liquidation_fuel": object(),
            "cross_exchange_resonance": object(),
            "imbalance": object(),
            "trend_exhaustion": object(),
        },
        signal_weights={
            "长期方向": {"multiplier": 1.25, "status": "boost", "sample_count": 100},
            "中期结构": {"multiplier": 0.65, "status": "reduce", "sample_count": 100},
        },
    )

    breakdown = result.risk_payload["score_breakdown"]
    assert breakdown["weight_mode"] == "governed"
    assert breakdown["weighted_score"] != pytest.approx(result.score)
    assert result.decision == "observe"
