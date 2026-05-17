from __future__ import annotations

from app.domain.research_lineage.registry import (
    DARKFLOW_V2_SHADOW_STRATEGY,
    LEGACY_FEATURE_SHADOW_STRATEGY,
    core_darkflow_v2_lineage,
    infrastructure_only_lineage,
    is_legacy_control_lineage,
    legacy_feature_research_lineage,
    strategy_lineage,
)
from app.services.research_lineage import core_darkflow_v2_lineage as service_core_darkflow_v2_lineage


def test_lineage_registry_marks_core_and_legacy_paths() -> None:
    core = core_darkflow_v2_lineage()
    legacy = legacy_feature_research_lineage()

    assert core["lineage"] == "core_darkflow_v2"
    assert core["is_primary_darkflow_path"] is True
    assert core["legacy_control"] is False
    assert legacy["lineage"] == "legacy_feature_research"
    assert legacy["is_primary_darkflow_path"] is False
    assert legacy["legacy_control"] is True
    assert is_legacy_control_lineage(legacy) is True


def test_strategy_lineage_keeps_legacy_shadow_out_of_core_path() -> None:
    assert strategy_lineage(LEGACY_FEATURE_SHADOW_STRATEGY)["legacy_control"] is True
    assert strategy_lineage(DARKFLOW_V2_SHADOW_STRATEGY)["is_primary_darkflow_path"] is True
    assert strategy_lineage("other") == infrastructure_only_lineage()


def test_service_lineage_helpers_delegate_to_registry() -> None:
    assert service_core_darkflow_v2_lineage() == core_darkflow_v2_lineage()
