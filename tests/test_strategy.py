from datetime import datetime, timezone

from app.models import SignalSnapshot
from app.services.strategy import _score_states, _state_from_snapshot, TimeframeState


def snapshot(payload: dict) -> object:
    return type("Snapshot", (), {"raw_payload": payload})()


def test_score_requires_long_term_bias() -> None:
    result = _score_states(
        "BTCUSDT",
        "core",
        100.0,
        [
            TimeframeState("short", "30m", "long", 100.0, 0.0, "Ongoing", "s1"),
            TimeframeState("mid", "1h", "long", 100.0, 0.0, "Ongoing", "s2"),
            TimeframeState("long", "4h", "missing", None, None, None, None),
        ],
        {},
    )

    assert result.decision == "observe"
    assert result.score == 0


def test_score_opens_when_three_timeframes_align_near_cost() -> None:
    result = _score_states(
        "BTCUSDT",
        "core",
        100.0,
        [
            TimeframeState("short", "30m", "long", 100.0, 0.0, "Ongoing", "s1"),
            TimeframeState("mid", "1h", "long", 100.0, 0.0, "Ongoing", "s2"),
            TimeframeState("long", "4h", "long", 100.0, 0.0, "Ongoing", "s3"),
        ],
        {
            "smart_money_cost": object(),
            "liq_heatmap": snapshot(
                {"heatmap_data": [{"price": 98.8, "intensity": 1.0}, {"price": 103.0, "intensity": 1.0}]}
            ),
            "liquidation_fuel": object(),
            "liquidity_sweep": object(),
            "cross_exchange_resonance": object(),
            "imbalance": object(),
            "trend_exhaustion": object(),
        },
    )

    assert result.decision == "open"
    assert result.score >= 75
    assert result.risk_payload["execution_gate"]["ready"] is True


def test_score_observes_when_price_is_outside_entry_zone() -> None:
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
            "liq_heatmap": snapshot(
                {"heatmap_data": [{"price": 98.8, "intensity": 1.0}, {"price": 106.0, "intensity": 1.0}]}
            ),
            "cross_exchange_resonance": object(),
            "imbalance": object(),
            "trend_exhaustion": object(),
        },
    )

    assert result.decision == "observe"
    assert result.risk_payload["entry_zone"]["inside"] is False


def test_short_execution_zone_excludes_entries_below_target_or_without_minimum_r() -> None:
    result = _score_states(
        "HYPEUSDT",
        "high_volatility",
        44.37,
        [
            TimeframeState("short", "30m", "short", 43.3525, 0.0235, "Ongoing", "s1"),
            TimeframeState("mid", "1h", "short", 43.7, 0.0153, "Ongoing", "s2"),
            TimeframeState("long", "4h", "short", 43.428167, 0.0217, "Ongoing", "s3"),
        ],
        {
            "smart_money_cost": object(),
            "liq_heatmap": snapshot(
                {
                    "heatmap_data": [
                        {"price": 45.00, "intensity": 1.0},
                        {"price": 43.428167, "intensity": 1.0},
                    ]
                }
            ),
            "liquidation_fuel": object(),
            "liquidity_sweep": object(),
            "cross_exchange_resonance": object(),
            "imbalance": object(),
            "trend_exhaustion": object(),
        },
    )

    zone = result.risk_payload["execution_zone"]
    assert zone["valid"] is True
    assert zone["lower"] > result.risk_payload["entry_zone"]["lower"]
    assert zone["lower"] > result.risk_payload["take_profit"]
    assert zone["upper"] < result.risk_payload["stop_loss"]
    assert result.decision == "open"


def test_short_observes_when_dark_flow_target_cannot_clear_minimum_r() -> None:
    result = _score_states(
        "HYPEUSDT",
        "high_volatility",
        43.65,
        [
            TimeframeState("short", "30m", "short", 43.3525, 0.0103, "Ongoing", "s1"),
            TimeframeState("mid", "1h", "short", 43.7, 0.0023, "Ongoing", "s2"),
            TimeframeState("long", "4h", "short", 43.428167, 0.0086, "Ongoing", "s3"),
        ],
        {
            "smart_money_cost": object(),
            "liq_heatmap": snapshot(
                {
                    "heatmap_data": [
                        {"price": 45.00, "intensity": 1.0},
                        {"price": 43.428167, "intensity": 1.0},
                    ]
                }
            ),
            "liquidation_fuel": object(),
            "liquidity_sweep": object(),
            "cross_exchange_resonance": object(),
            "imbalance": object(),
            "trend_exhaustion": object(),
        },
    )

    assert result.risk_payload["entry_zone"]["inside"] is True
    assert result.risk_payload["target_source"] == "risk_reward_template"
    assert result.decision == "observe"


def test_score_observes_when_risk_uses_fixed_fallbacks() -> None:
    result = _score_states(
        "BTCUSDT",
        "core",
        100.0,
        [
            TimeframeState("short", "30m", "long", 100.0, 0.0, "Ongoing", "s1"),
            TimeframeState("mid", "1h", "long", 100.0, 0.0, "Ongoing", "s2"),
            TimeframeState("long", "4h", "long", 100.0, 0.0, "Ongoing", "s3"),
        ],
        {
            "smart_money_cost": object(),
            "liq_heatmap": object(),
            "cross_exchange_resonance": object(),
            "imbalance": object(),
            "trend_exhaustion": object(),
        },
    )

    assert result.decision == "observe"
    assert result.risk_payload["execution_gate"]["ready"] is False


def test_score_observes_when_required_indicators_are_missing() -> None:
    result = _score_states(
        "BTCUSDT",
        "core",
        100.0,
        [
            TimeframeState("short", "30m", "long", 100.0, 0.0, "Ongoing", "s1"),
            TimeframeState("mid", "1h", "long", 100.0, 0.0, "Ongoing", "s2"),
            TimeframeState("long", "4h", "long", 100.0, 0.0, "Ongoing", "s3"),
        ],
        {"smart_money_cost": object()},
    )

    assert result.decision == "observe"
    assert "核心指标缺失" in " ".join(result.reason["warnings"])


def test_stale_cost_snapshot_does_not_create_directional_bias() -> None:
    snapshot = SignalSnapshot(
        symbol="BTCUSDT",
        asset_tier="core",
        timeframe="short",
        interval="30m",
        indicator="smart_money_cost",
        endpoint="/api/pro/pro_data",
        raw_payload={"smart_money_cost": [{"type": "Accumulation", "avg_price": 100}]},
        summary_payload={},
        collected_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
    )

    state = _state_from_snapshot(snapshot, "short", "30m", 100.0)

    assert state.bias == "stale"
    assert state.is_stale


def test_score_observes_when_long_term_state_is_stale() -> None:
    result = _score_states(
        "BTCUSDT",
        "core",
        100.0,
        [
            TimeframeState("short", "30m", "long", 100.0, 0.0, "Ongoing", "s1"),
            TimeframeState("mid", "1h", "long", 100.0, 0.0, "Ongoing", "s2"),
            TimeframeState("long", "4h", "stale", None, None, "stale", "s3", True, 999.0),
        ],
        {
            "smart_money_cost": object(),
            "liq_heatmap": object(),
            "cross_exchange_resonance": object(),
            "imbalance": object(),
            "trend_exhaustion": object(),
        },
        stale_indicators=["smart_money_cost"],
    )

    assert result.decision == "observe"
    assert result.score == 0
    assert result.reason["error"] == "missing_or_stale_long_bias"
