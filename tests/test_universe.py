from app.constants import (
    ASSETS,
    COLLECTABLE_INDICATORS,
    CORE_INDICATORS,
    EXPERIMENT_INDICATORS,
    HFD_INDICATORS,
    REQUIRED_SCORING_INDICATORS,
    RESEARCH_INDICATORS,
    TIMEFRAMES,
)
from app.services.collector import normalize_assets, normalize_indicators, normalize_timeframes


def test_requested_assets_are_present() -> None:
    for symbol in ("BTC", "ETH", "SOL", "BNB", "LINK", "TON", "DOGE", "HYPE", "ZEC"):
        assert symbol in ASSETS


def test_timeframe_mapping_matches_hfd_pro() -> None:
    assert TIMEFRAMES["short"].interval == "30m"
    assert TIMEFRAMES["mid"].interval == "1h"
    assert TIMEFRAMES["long"].interval == "4h"


def test_core_indicators_include_signal_families() -> None:
    assert "smart_money_cost" in CORE_INDICATORS
    assert "liq_heatmap" in CORE_INDICATORS
    assert "cross_exchange_resonance" in CORE_INDICATORS


def test_hfd_indicator_catalog_maps_darkflow_names() -> None:
    assert HFD_INDICATORS["fair_value_gap"].hfd_name == "筹码真空区"
    assert HFD_INDICATORS["cascade_liquidation_zones"].hfd_name == "连环爆仓区"
    assert HFD_INDICATORS["retail_stop_loss"].hfd_name == "散户止损点"
    assert HFD_INDICATORS["inst_choch"].hfd_name == "破坏与突破"
    assert HFD_INDICATORS["smart_money_cost"].status == "scoring"


def test_experiment_indicators_are_collectable_but_not_core() -> None:
    assert "fair_value_gap" in EXPERIMENT_INDICATORS
    assert "inst_choch" in EXPERIMENT_INDICATORS
    assert set(EXPERIMENT_INDICATORS).isdisjoint(CORE_INDICATORS)
    assert set(EXPERIMENT_INDICATORS).issubset(COLLECTABLE_INDICATORS)
    assert HFD_INDICATORS["fair_value_gap"].status == "experiment"


def test_research_indicators_are_non_scoring_core_indicators() -> None:
    assert set(RESEARCH_INDICATORS).isdisjoint(REQUIRED_SCORING_INDICATORS)
    assert set(RESEARCH_INDICATORS) | set(REQUIRED_SCORING_INDICATORS) == set(CORE_INDICATORS)
    assert "liquidity_sweep" in RESEARCH_INDICATORS


def test_normalizers_reject_unknown_values() -> None:
    try:
        normalize_assets(["BTC", "NOPE"])
    except ValueError as exc:
        assert "NOPE" in str(exc)
    else:
        raise AssertionError("normalize_assets should reject unknown assets")

    try:
        normalize_timeframes(["short", "daily"])
    except ValueError as exc:
        assert "daily" in str(exc)
    else:
        raise AssertionError("normalize_timeframes should reject unknown timeframes")


def test_normalize_indicators_accepts_experiments_and_rejects_catalog_only() -> None:
    assert normalize_indicators(["fair_value_gap", "smart_money_cost"]) == [
        "fair_value_gap",
        "smart_money_cost",
    ]

    try:
        normalize_indicators(["max_pain"])
    except ValueError as exc:
        assert "max_pain" in str(exc)
    else:
        raise AssertionError("normalize_indicators should reject non-selected catalog indicators")
