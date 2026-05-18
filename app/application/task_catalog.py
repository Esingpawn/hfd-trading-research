from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.domain.research_lineage.registry import CORE_DARKFLOW_V2, INFRASTRUCTURE_ONLY, LEGACY_FEATURE_RESEARCH


@dataclass(frozen=True)
class TaskSpec:
    canonical_name: str
    aliases: tuple[str, ...]
    lineage: str
    production_allowed: bool
    heavy: bool = False

    def matches(self, name: str) -> bool:
        raw = str(name or "").strip()
        accepted = {self.canonical_name, *self.aliases}
        normalized_accepted = {normalize_task_name(item) for item in accepted}
        return raw in accepted or normalize_task_name(raw) in normalized_accepted


TASK_SPECS: tuple[TaskSpec, ...] = (
    TaskSpec("collect.run", ("collect",), INFRASTRUCTURE_ONLY, True),
    TaskSpec("collect.prices", ("collect-prices",), INFRASTRUCTURE_ONLY, True),
    TaskSpec("collect.scoring_core", ("collect-scoring-core",), INFRASTRUCTURE_ONLY, True),
    TaskSpec("paper.scan", ("paper-scan",), INFRASTRUCTURE_ONLY, True),
    TaskSpec("paper.mark", ("paper-mark",), INFRASTRUCTURE_ONLY, True),
    TaskSpec("shadow_paper.scan", ("shadow-paper-scan",), LEGACY_FEATURE_RESEARCH, False),
    TaskSpec("shadow_paper.replay", ("shadow-paper-replay",), LEGACY_FEATURE_RESEARCH, False, heavy=True),
    TaskSpec("shadow_paper.replay_all", ("shadow-paper-replay-all",), LEGACY_FEATURE_RESEARCH, False, heavy=True),
    TaskSpec("shadow_paper.mark", ("shadow-paper-mark",), LEGACY_FEATURE_RESEARCH, False),
    TaskSpec("shadow_paper.promotion", ("shadow-paper-promotion",), LEGACY_FEATURE_RESEARCH, False),
    TaskSpec("research.accelerate", ("research-accelerate",), LEGACY_FEATURE_RESEARCH, False, heavy=True),
    TaskSpec("tasks.reap_stale", ("tasks-reap-stale",), INFRASTRUCTURE_ONLY, True),
    TaskSpec("signals.backfill", ("signals-backfill",), INFRASTRUCTURE_ONLY, True, heavy=True),
    TaskSpec("features.backfill", ("features-backfill",), LEGACY_FEATURE_RESEARCH, False, heavy=True),
    TaskSpec("features.label", ("features-label",), LEGACY_FEATURE_RESEARCH, False, heavy=True),
    TaskSpec("features.reset", ("features-reset",), LEGACY_FEATURE_RESEARCH, False, heavy=True),
    TaskSpec("features.refresh", ("features-refresh",), LEGACY_FEATURE_RESEARCH, False, heavy=True),
    TaskSpec("features.candidates", ("features-candidates",), LEGACY_FEATURE_RESEARCH, False, heavy=True),
    TaskSpec("features.paper_ab", ("features-paper-ab",), LEGACY_FEATURE_RESEARCH, False, heavy=True),
    TaskSpec("features.segment_candidates", ("features-segment-candidates",), LEGACY_FEATURE_RESEARCH, False, heavy=True),
    TaskSpec("features.segment_paper_ab", ("features-segment-paper-ab",), LEGACY_FEATURE_RESEARCH, False, heavy=True),
    TaskSpec("features.research_reports", ("features-research-reports",), LEGACY_FEATURE_RESEARCH, False, heavy=True),
    TaskSpec("darkflow.playbook_backtest", ("darkflow-playbook-backtest",), CORE_DARKFLOW_V2, True, heavy=True),
    TaskSpec("darkflow.zones_backfill", ("darkflow-zones-backfill",), CORE_DARKFLOW_V2, True, heavy=True),
    TaskSpec("darkflow.interactions_backfill", ("darkflow-interactions-backfill",), CORE_DARKFLOW_V2, True, heavy=True),
    TaskSpec("darkflow.interaction_backtest", ("darkflow-interaction-backtest",), CORE_DARKFLOW_V2, True, heavy=True),
    TaskSpec("darkflow.trade_candidates", ("darkflow-trade-candidates",), CORE_DARKFLOW_V2, True),
    TaskSpec("darkflow.trade_candidate_audit", ("darkflow-trade-candidate-audit",), CORE_DARKFLOW_V2, True),
    TaskSpec("darkflow.trade_candidate_shadow_forward", ("darkflow-trade-candidate-shadow-forward",), CORE_DARKFLOW_V2, True),
    TaskSpec("darkflow.trade_candidate_promotion", ("darkflow-trade-candidate-promotion",), CORE_DARKFLOW_V2, True),
    TaskSpec("darkflow.trade_candidate_promotion_report", ("darkflow-trade-candidate-promotion-report",), CORE_DARKFLOW_V2, True),
    TaskSpec("darkflow.alpha_scoreboard", ("darkflow-alpha-scoreboard",), CORE_DARKFLOW_V2, True),
    TaskSpec("darkflow.alpha_accelerate", ("darkflow-alpha-accelerate",), CORE_DARKFLOW_V2, True),
    TaskSpec("darkflow.waiting_refresh", ("darkflow-waiting-refresh",), CORE_DARKFLOW_V2, True),
    TaskSpec("darkflow.shadow_replay", ("darkflow-shadow-replay",), CORE_DARKFLOW_V2, False, heavy=True),
    TaskSpec("data_quality.report", ("data-quality-report",), INFRASTRUCTURE_ONLY, True),
    TaskSpec("storage.maintain", ("storage-maintain",), INFRASTRUCTURE_ONLY, True, heavy=True),
)


def normalize_task_name(name: str) -> str:
    return str(name or "").strip().replace("-", ".").replace("_", ".")


def task_spec(name: str) -> TaskSpec | None:
    for spec in TASK_SPECS:
        if spec.matches(name):
            return spec
    return None


def task_catalog_payload(specs: Iterable[TaskSpec] = TASK_SPECS) -> list[dict[str, object]]:
    return [
        {
            "canonical_name": item.canonical_name,
            "aliases": list(item.aliases),
            "lineage": item.lineage,
            "production_allowed": item.production_allowed,
            "heavy": item.heavy,
        }
        for item in specs
    ]
