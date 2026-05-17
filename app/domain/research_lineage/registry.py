from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CORE_DARKFLOW_V2 = "core_darkflow_v2"
LEGACY_FEATURE_RESEARCH = "legacy_feature_research"
LEGACY_BASELINE_V0 = "legacy_baseline_v0"
INFRASTRUCTURE_ONLY = "infrastructure_only"
LEGACY_FEATURE_SHADOW_STRATEGY = "shadow_feature_candidates_v1"
DARKFLOW_V2_SHADOW_STRATEGY = "darkflow_v2_trade_candidate_shadow_forward_v1"


@dataclass(frozen=True)
class ResearchLineage:
    lineage: str
    is_primary_darkflow_path: bool
    legacy_control: bool
    decision_path: str
    promotion_boundary: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "lineage": self.lineage,
            "is_primary_darkflow_path": self.is_primary_darkflow_path,
            "legacy_control": self.legacy_control,
            "decision_path": self.decision_path,
            "promotion_boundary": self.promotion_boundary,
        }


def core_darkflow_v2_lineage() -> dict[str, Any]:
    return ResearchLineage(
        lineage=CORE_DARKFLOW_V2,
        is_primary_darkflow_path=True,
        legacy_control=False,
        decision_path="raw_snapshot -> DarkflowZone -> DarkflowInteraction -> TradeCandidate/DecisionCard",
        promotion_boundary="Requires anti-repaint audit, decision-card gates, and isolated v2 shadow-paper before real paper/live.",
    ).as_payload()


def legacy_feature_research_lineage() -> dict[str, Any]:
    return ResearchLineage(
        lineage=LEGACY_FEATURE_RESEARCH,
        is_primary_darkflow_path=False,
        legacy_control=True,
        decision_path="generic FeatureEvent/FeatureLabel research control",
        promotion_boundary="Not eligible for opening decisions or execution weights without conversion into official darkflow TradeCandidate/DecisionCard flow.",
    ).as_payload()


def legacy_baseline_v0_lineage() -> dict[str, Any]:
    return ResearchLineage(
        lineage=LEGACY_BASELINE_V0,
        is_primary_darkflow_path=False,
        legacy_control=True,
        decision_path="baseline_v0 infrastructure/control evidence",
        promotion_boundary="Cannot be used for opening decisions; kept only for infrastructure comparison and historical control.",
    ).as_payload()


def infrastructure_only_lineage() -> dict[str, Any]:
    return ResearchLineage(
        lineage=INFRASTRUCTURE_ONLY,
        is_primary_darkflow_path=False,
        legacy_control=False,
        decision_path="infrastructure or data-quality support",
        promotion_boundary="Not a strategy signal path.",
    ).as_payload()


def strategy_lineage(strategy_name: str | None) -> dict[str, Any]:
    if strategy_name == LEGACY_FEATURE_SHADOW_STRATEGY:
        return legacy_feature_research_lineage()
    if strategy_name == DARKFLOW_V2_SHADOW_STRATEGY:
        return core_darkflow_v2_lineage()
    return infrastructure_only_lineage()


def is_legacy_control_lineage(payload: dict[str, Any] | None) -> bool:
    return bool(isinstance(payload, dict) and payload.get("legacy_control") is True)
