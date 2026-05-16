from __future__ import annotations

from typing import Any


CORE_DARKFLOW_V2 = "core_darkflow_v2"
LEGACY_FEATURE_RESEARCH = "legacy_feature_research"
LEGACY_BASELINE_V0 = "legacy_baseline_v0"
INFRASTRUCTURE_ONLY = "infrastructure_only"


def core_darkflow_v2_lineage() -> dict[str, Any]:
    return {
        "lineage": CORE_DARKFLOW_V2,
        "is_primary_darkflow_path": True,
        "legacy_control": False,
        "decision_path": "raw_snapshot -> DarkflowZone -> DarkflowInteraction -> TradeCandidate/DecisionCard",
        "promotion_boundary": "Requires anti-repaint audit, decision-card gates, and isolated v2 shadow-paper before real paper/live.",
    }


def legacy_feature_research_lineage() -> dict[str, Any]:
    return {
        "lineage": LEGACY_FEATURE_RESEARCH,
        "is_primary_darkflow_path": False,
        "legacy_control": True,
        "decision_path": "generic FeatureEvent/FeatureLabel research control",
        "promotion_boundary": "Not eligible for opening decisions or execution weights without conversion into official darkflow TradeCandidate/DecisionCard flow.",
    }


def legacy_baseline_v0_lineage() -> dict[str, Any]:
    return {
        "lineage": LEGACY_BASELINE_V0,
        "is_primary_darkflow_path": False,
        "legacy_control": True,
        "decision_path": "baseline_v0 infrastructure/control evidence",
        "promotion_boundary": "Cannot be used for opening decisions; kept only for infrastructure comparison and historical control.",
    }


def infrastructure_only_lineage() -> dict[str, Any]:
    return {
        "lineage": INFRASTRUCTURE_ONLY,
        "is_primary_darkflow_path": False,
        "legacy_control": False,
        "decision_path": "infrastructure or data-quality support",
        "promotion_boundary": "Not a strategy signal path.",
    }
