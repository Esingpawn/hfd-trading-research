from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


PROMOTION_BLOCKER_ANTI_REPAINT_MISSING = "anti_repaint_audit_missing"
PROMOTION_BLOCKER_ANTI_REPAINT_FAILED = "anti_repaint_audit_failed"
PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING = "isolated_v2_shadow_forward_sample_missing"
PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING = "isolated_v2_shadow_forward_sample_collecting"
PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED = "isolated_v2_shadow_forward_sample_failed"
PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED = "entry_plan_retired"
PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN = "duplicate_shadow_forward_plan"
PROMOTION_BLOCKER_PERSISTENT_TABLE_MISSING = "persistent_trade_candidate_table_missing"
PROMOTION_BLOCKER_SHADOW_MARKET_PAUSED = "shadow_market_performance_paused"


@dataclass(frozen=True)
class AntiRepaintEvidence:
    status: str = "missing"


@dataclass(frozen=True)
class ShadowForwardEvidence:
    status: str = "not_started"


@dataclass(frozen=True)
class CandidateLifecycleEvidence:
    status: str = "research_blocked"
    anti_repaint: AntiRepaintEvidence = field(default_factory=AntiRepaintEvidence)
    shadow: ShadowForwardEvidence = field(default_factory=ShadowForwardEvidence)
    promotion_blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateLifecycleDecision:
    status: str
    anti_repaint_status: str
    shadow_status: str
    promotion_status: str
    promotion_blockers: list[str]
    paper_eligible: bool = False
    live_eligible: bool = False


def decide_candidate_lifecycle(evidence: CandidateLifecycleEvidence) -> CandidateLifecycleDecision:
    blockers = normalized_promotion_blockers(evidence.promotion_blockers)
    blockers = lifecycle_blockers(
        blockers,
        anti_repaint_status=evidence.anti_repaint.status,
        shadow_status=evidence.shadow.status,
    )
    promotion_status = candidate_promotion_status(
        status=evidence.status,
        anti_repaint_status=evidence.anti_repaint.status,
        shadow_status=evidence.shadow.status,
        promotion_blockers=blockers,
    )
    return CandidateLifecycleDecision(
        status=evidence.status,
        anti_repaint_status=evidence.anti_repaint.status,
        shadow_status=evidence.shadow.status,
        promotion_status=promotion_status,
        promotion_blockers=blockers,
        paper_eligible=False,
        live_eligible=False,
    )


def lifecycle_blockers(
    promotion_blockers: Iterable[object],
    *,
    anti_repaint_status: str,
    shadow_status: str,
) -> list[str]:
    blockers = set(normalized_promotion_blockers(promotion_blockers))
    if anti_repaint_status == "passed":
        blockers.discard(PROMOTION_BLOCKER_ANTI_REPAINT_MISSING)
        blockers.discard(PROMOTION_BLOCKER_ANTI_REPAINT_FAILED)
    elif anti_repaint_status == "failed":
        blockers.discard(PROMOTION_BLOCKER_ANTI_REPAINT_MISSING)
        blockers.add(PROMOTION_BLOCKER_ANTI_REPAINT_FAILED)
    else:
        blockers.add(PROMOTION_BLOCKER_ANTI_REPAINT_MISSING)

    if shadow_status == "passed":
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED)
        blockers.discard(PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED)
        blockers.discard(PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN)
    elif shadow_status == "collecting":
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED)
        blockers.discard(PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED)
        blockers.discard(PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN)
        blockers.add(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING)
    elif shadow_status == "failed":
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING)
        blockers.add(PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED)
    elif shadow_status == "retired":
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING)
        blockers.discard(PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED)
        if PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN in blockers or PROMOTION_BLOCKER_SHADOW_MARKET_PAUSED in blockers:
            blockers.discard(PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED)
        else:
            blockers.add(PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED)
    else:
        blockers.discard(PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED)
        blockers.discard(PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN)
        blockers.add(PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING)
    return ordered_promotion_blockers(blockers)


def candidate_promotion_status(
    *,
    status: str,
    anti_repaint_status: str,
    shadow_status: str,
    promotion_blockers: Iterable[object],
) -> str:
    blockers = set(normalized_promotion_blockers(promotion_blockers))
    if PROMOTION_BLOCKER_SHADOW_MARKET_PAUSED in blockers:
        return "shadow_market_paused"
    if PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN in blockers:
        return "duplicate_shadow_plan"
    if shadow_status == "retired" or PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED in blockers:
        return "entry_plan_retired"
    if status != "shadow_candidate":
        return "blocked"
    if anti_repaint_status == "failed" or PROMOTION_BLOCKER_ANTI_REPAINT_FAILED in blockers:
        return "anti_repaint_failed"
    if anti_repaint_status != "passed":
        return "anti_repaint_pending"
    if shadow_status == "failed" or PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED in blockers:
        return "shadow_forward_failed"
    if shadow_status == "collecting" or PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING in blockers:
        return "shadow_forward_collecting"
    if shadow_status != "passed":
        return "shadow_forward_pending"
    return "paper_review_ready"


def normalized_promotion_blockers(values: Iterable[object]) -> list[str]:
    obsolete = {PROMOTION_BLOCKER_PERSISTENT_TABLE_MISSING}
    blockers = [str(value) for value in values if str(value) not in obsolete]
    return list(dict.fromkeys(blockers))


def ordered_promotion_blockers(blockers: Iterable[str]) -> list[str]:
    blocker_set = set(blockers)
    ordered = [
        PROMOTION_BLOCKER_ANTI_REPAINT_MISSING,
        PROMOTION_BLOCKER_ANTI_REPAINT_FAILED,
        PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING,
        PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING,
        PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED,
        PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED,
        PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN,
        PROMOTION_BLOCKER_SHADOW_MARKET_PAUSED,
    ]
    return [item for item in ordered if item in blocker_set] + sorted(blocker_set - set(ordered))
