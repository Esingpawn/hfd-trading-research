from __future__ import annotations

import pytest

from app.domain.whitelist_blacklist_policy import classify_setup_expectancy, strategy_action_from_expectancy


def row(**overrides):
    payload = {
        "evidence_source": "shadow_forward",
        "closed_trades": 6,
        "sample_count": 6,
        "valid_outcome_trades": 6,
        "invalid_outcome_trades": 0,
        "win_rate": 0.66,
        "profit_factor": 1.8,
        "max_drawdown": 0.04,
        "time_exit_share": 0.2,
    }
    payload.update(overrides)
    return payload


def test_policy_whitelists_positive_core_darkflow_expectancy() -> None:
    decision = classify_setup_expectancy(row())

    assert decision["classification"] == "whitelist"
    assert decision["sampling_action"] == "prioritize"
    assert decision["display_text"] == "白名单补样"
    assert "positive_expectancy_passed" in decision["reason_codes"]


def test_policy_collects_until_minimum_samples() -> None:
    decision = classify_setup_expectancy(row(closed_trades=2, sample_count=2))

    assert decision["classification"] == "collecting"
    assert decision["sampling_action"] == "prioritize"
    assert "insufficient_closed_samples" in decision["reason_codes"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"win_rate": 0.3, "profit_factor": 0.8, "time_exit_share": 0.2}, "pause"),
        ({"win_rate": 0.2, "profit_factor": 0.45, "time_exit_share": 0.2}, "blacklist"),
        ({"win_rate": 0.6, "profit_factor": 1.2, "max_drawdown": 0.2}, "pause"),
        ({"win_rate": 0.42, "profit_factor": 0.9, "time_exit_share": 0.8}, "pause"),
    ],
)
def test_policy_classifies_weak_pf_drawdown_and_time_exit(payload, expected) -> None:
    decision = classify_setup_expectancy(row(**payload))

    assert decision["classification"] == expected
    assert decision["sampling_action"] in {"pause", "block"}


def test_policy_blocks_legacy_control_from_whitelist() -> None:
    decision = classify_setup_expectancy(row(evidence_source="legacy_control"))

    assert decision["classification"] == "collecting"
    assert decision["can_promote"] is False
    assert "non_core_darkflow_evidence" in decision["reason_codes"]


def test_policy_flags_invalid_outcome_quality() -> None:
    decision = classify_setup_expectancy(row(closed_trades=8, sample_count=5, invalid_outcome_trades=3))

    assert decision["classification"] == "pause"
    assert decision["can_promote"] is False
    assert "invalid_outcome_ratio_high" in decision["reason_codes"]


def test_strategy_action_deweights_weak_strategy_expectancy() -> None:
    action = strategy_action_from_expectancy(row(closed_trades=12, sample_count=12, profit_factor=0.8, win_rate=0.44))

    assert action["main_path_action"] == "deweight"
    assert action["weight_multiplier"] < 1.0
    assert "strategy_expectancy_weak" in action["reason_codes"]
