from app.services.darkflow_playbooks import darkflow_playbook_catalog
from app.services.darkflow_rules import darkflow_rulebook, official_rule_for_internal_indicator


def test_darkflow_rulebook_exposes_official_indicator_mapping() -> None:
    report = darkflow_rulebook()

    assert report["strategy_family"] == "darkflow_tutorial_semantics_v1"
    assert report["indicator_count"] >= 30
    assert report["policy"]["baseline_v0_status"] == "infrastructure_and_control_only"
    assert report["policy"]["lineage"]["lineage"] == "core_darkflow_v2"
    assert report["policy"]["baseline_v0_lineage"]["lineage"] == "legacy_baseline_v0"
    assert "fair_value_gap" in report["official_to_internal"]["fvg"]
    assert "liquidity_vacuum" in report["official_to_internal"]["liq_vacuum"]


def test_official_rule_lookup_by_internal_indicator() -> None:
    rule = official_rule_for_internal_indicator("liquidity_sweep")

    assert rule is not None
    assert rule.official_key == "liquidity_sweep"
    assert rule.single_trigger_allowed is True
    assert "liq_heatmap" in rule.confirmation_required


def test_darkflow_playbook_catalog_is_research_only() -> None:
    catalog = darkflow_playbook_catalog()

    assert catalog["policy"]["opens_live_orders"] is False
    assert catalog["policy"]["opens_paper_trades"] is False
    assert catalog["policy"]["lineage"]["lineage"] == "core_darkflow_v2"
    assert catalog["playbook_count"] >= 6
    assert {item["key"] for item in catalog["playbooks"]} >= {
        "pullback_to_cost",
        "liquidity_sweep_reversal",
        "trend_ride_extension",
    }
