from __future__ import annotations

from typing import Any

from app.domain.research_lineage.registry import (
    CORE_DARKFLOW_V2,
    INFRASTRUCTURE_ONLY,
    LEGACY_BASELINE_V0,
    LEGACY_FEATURE_RESEARCH,
    core_darkflow_v2_lineage as _core_darkflow_v2_lineage,
    infrastructure_only_lineage as _infrastructure_only_lineage,
    legacy_baseline_v0_lineage as _legacy_baseline_v0_lineage,
    legacy_feature_research_lineage as _legacy_feature_research_lineage,
)


def core_darkflow_v2_lineage() -> dict[str, Any]:
    return _core_darkflow_v2_lineage()


def legacy_feature_research_lineage() -> dict[str, Any]:
    return _legacy_feature_research_lineage()


def legacy_baseline_v0_lineage() -> dict[str, Any]:
    return _legacy_baseline_v0_lineage()


def infrastructure_only_lineage() -> dict[str, Any]:
    return _infrastructure_only_lineage()
