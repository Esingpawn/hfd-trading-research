from __future__ import annotations

from app.domain.trade_candidates.lifecycle import (
    AntiRepaintEvidence,
    CandidateLifecycleEvidence,
    PROMOTION_BLOCKER_ANTI_REPAINT_FAILED,
    PROMOTION_BLOCKER_ANTI_REPAINT_MISSING,
    PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN,
    PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED,
    PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING,
    PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED,
    PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING,
    ShadowForwardEvidence,
    decide_candidate_lifecycle,
)


def _decision(
    *,
    status: str = "shadow_candidate",
    anti_repaint_status: str = "missing",
    shadow_status: str = "not_started",
    blockers: tuple[str, ...] = (),
):
    return decide_candidate_lifecycle(
        CandidateLifecycleEvidence(
            status=status,
            anti_repaint=AntiRepaintEvidence(anti_repaint_status),
            shadow=ShadowForwardEvidence(shadow_status),
            promotion_blockers=blockers,
        )
    )


def test_lifecycle_waits_for_missing_anti_repaint_audit() -> None:
    decision = _decision()

    assert decision.promotion_status == "anti_repaint_pending"
    assert PROMOTION_BLOCKER_ANTI_REPAINT_MISSING in decision.promotion_blockers
    assert PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING in decision.promotion_blockers
    assert decision.paper_eligible is False
    assert decision.live_eligible is False


def test_lifecycle_blocks_failed_anti_repaint_audit() -> None:
    decision = _decision(anti_repaint_status="failed")

    assert decision.promotion_status == "anti_repaint_failed"
    assert decision.promotion_blockers[0] == PROMOTION_BLOCKER_ANTI_REPAINT_FAILED
    assert PROMOTION_BLOCKER_ANTI_REPAINT_MISSING not in decision.promotion_blockers


def test_lifecycle_waits_for_shadow_forward_after_anti_repaint_passes() -> None:
    decision = _decision(anti_repaint_status="passed")

    assert decision.promotion_status == "shadow_forward_pending"
    assert PROMOTION_BLOCKER_ANTI_REPAINT_MISSING not in decision.promotion_blockers
    assert decision.promotion_blockers == [PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING]


def test_lifecycle_tracks_collecting_shadow_forward_samples() -> None:
    decision = _decision(anti_repaint_status="passed", shadow_status="collecting")

    assert decision.promotion_status == "shadow_forward_collecting"
    assert decision.promotion_blockers == [PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING]


def test_lifecycle_blocks_failed_shadow_forward_samples() -> None:
    decision = _decision(anti_repaint_status="passed", shadow_status="failed")

    assert decision.promotion_status == "shadow_forward_failed"
    assert decision.promotion_blockers == [PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED]


def test_lifecycle_reaches_paper_review_ready_after_shadow_passes() -> None:
    decision = _decision(anti_repaint_status="passed", shadow_status="passed")

    assert decision.promotion_status == "paper_review_ready"
    assert decision.promotion_blockers == []
    assert decision.paper_eligible is False
    assert decision.live_eligible is False


def test_lifecycle_retires_entry_plan() -> None:
    decision = _decision(
        anti_repaint_status="passed",
        shadow_status="retired",
        blockers=(PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED,),
    )

    assert decision.promotion_status == "entry_plan_retired"
    assert decision.shadow_status == "retired"
    assert decision.promotion_blockers == [PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED]


def test_lifecycle_retires_duplicate_shadow_plan_without_entry_plan_blocker() -> None:
    decision = _decision(
        anti_repaint_status="passed",
        shadow_status="retired",
        blockers=(PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN,),
    )

    assert decision.promotion_status == "duplicate_shadow_plan"
    assert decision.shadow_status == "retired"
    assert decision.promotion_blockers == [PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN]
    assert PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED not in decision.promotion_blockers
