from __future__ import annotations

from app.domain.trade_candidates.lifecycle import (
    PROMOTION_BLOCKER_ANTI_REPAINT_MISSING,
    PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN,
    PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED,
    PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING,
)
from app.domain.trade_candidates.promotion_gate import PromotionGateEvidence, decide_promotion_gate, promotion_gate_policy


def _decision(
    *,
    candidate_key: str = "candidate-1",
    lineage: str = "core_darkflow_v2",
    status: str = "shadow_candidate",
    promotion_status: str = "shadow_forward_pending",
    anti_repaint_status: str = "passed",
    shadow_status: str = "not_started",
    blockers: tuple[str, ...] = (),
    entry_plan_state: dict | None = None,
    shadow_stats: dict | None = None,
):
    return decide_promotion_gate(
        PromotionGateEvidence(
            candidate_key=candidate_key,
            lineage=lineage,
            status=status,
            promotion_status=promotion_status,
            anti_repaint_status=anti_repaint_status,
            shadow_status=shadow_status,
            promotion_blockers=blockers,
            entry_plan_state=entry_plan_state or {"state": "triggered", "reason": "mark_price_inside_frozen_entry_range"},
            shadow_stats=shadow_stats or {},
        )
    )


def test_gate_blocks_when_anti_repaint_evidence_is_missing() -> None:
    decision = _decision(
        promotion_status="anti_repaint_pending",
        anti_repaint_status="missing",
        blockers=(PROMOTION_BLOCKER_ANTI_REPAINT_MISSING,),
    )

    assert decision.gate_status == "blocked"
    assert decision.primary_blocker == PROMOTION_BLOCKER_ANTI_REPAINT_MISSING
    assert decision.blocker_groups["anti_repaint"][0]["severity"] == "blocker"
    assert "防重绘证据缺失" in decision.blocker_groups["anti_repaint"][0]["message"]


def test_gate_collects_shadow_forward_samples_without_blocking_evidence() -> None:
    decision = _decision(
        promotion_status="shadow_forward_collecting",
        shadow_status="collecting",
        blockers=(PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING,),
        shadow_stats={"closed_trades": 3, "win_rate": 0.67, "profit_factor": 1.4},
    )

    assert decision.gate_status == "collecting"
    assert decision.primary_blocker == PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING
    assert decision.blocker_groups["shadow_forward"][0]["severity"] == "waiting"
    assert decision.evidence_summary["shadow_closed_trades"] == 3


def test_gate_treats_low_rr_as_review_blocker_only_after_shadow_passes() -> None:
    collecting = _decision(
        promotion_status="shadow_forward_collecting",
        shadow_status="collecting",
        blockers=("rr_ratio_below_threshold", "isolated_v2_shadow_forward_sample_collecting"),
    )
    review = _decision(
        promotion_status="paper_review_ready",
        shadow_status="passed",
        blockers=("rr_ratio_below_threshold",),
    )

    assert collecting.gate_status == "collecting"
    assert collecting.blocker_groups["risk_shape"][0]["severity"] == "waiting"
    assert review.gate_status == "blocked"
    assert review.primary_blocker == "rr_ratio_below_threshold"
    assert review.blocker_groups["risk_shape"][0]["severity"] == "blocker"


def test_gate_explains_quality_and_trend_blockers_in_chinese() -> None:
    decision = _decision(
        blockers=(
            "quality_score_below_threshold",
            "parent_trend_conflict",
            "body_break_invalidation",
            "official_rule_unmapped",
            "exit_filter_not_opening_playbook",
        ),
    )

    messages = [item["message"] for item in decision.blocker_groups["risk_shape"]]
    assert all("存在未分类阻塞项" not in message for message in messages)
    assert any("质量评分" in message for message in messages)
    assert any("父级趋势" in message for message in messages)
    assert any("实体突破" in message for message in messages)
    assert any("教程规则" in message for message in messages)
    assert any("只作为离场" in message for message in messages)


def test_gate_watches_frozen_entry_plan_before_review_ready() -> None:
    decision = _decision(
        promotion_status="paper_review_ready",
        shadow_status="passed",
        entry_plan_state={"state": "waiting", "reason": "awaiting_frozen_entry_range"},
    )

    assert decision.gate_status == "watching_entry"
    assert decision.primary_blocker == "entry_plan_waiting"
    assert decision.blocker_groups["entry_plan"][0]["severity"] == "waiting"


def test_gate_marks_review_ready_only_after_evidence_and_entry_are_ready() -> None:
    decision = _decision(
        promotion_status="paper_review_ready",
        shadow_status="passed",
        shadow_stats={"closed_trades": 12, "win_rate": 0.58, "profit_factor": 1.3, "max_drawdown": 0.05},
    )

    assert decision.gate_status == "review_ready"
    assert decision.primary_blocker is None
    assert decision.blocker_groups == {}
    assert "人工复核" in decision.next_action


def test_gate_retires_entry_or_duplicate_plans() -> None:
    entry_retired = _decision(
        promotion_status="entry_plan_retired",
        shadow_status="retired",
        blockers=(PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED,),
    )
    duplicate_retired = _decision(
        promotion_status="duplicate_shadow_plan",
        shadow_status="retired",
        blockers=(PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN,),
    )

    assert entry_retired.gate_status == "retired"
    assert duplicate_retired.gate_status == "retired"
    assert duplicate_retired.primary_blocker == PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN


def test_gate_excludes_non_core_lineage() -> None:
    decision = _decision(lineage="legacy_control_research")

    assert decision.gate_status == "blocked"
    assert decision.primary_blocker == "non_core_darkflow_v2"
    assert decision.blocker_groups["lineage"][0]["severity"] == "blocker"


def test_gate_policy_is_report_only_and_stops_at_review_ready() -> None:
    policy = promotion_gate_policy()

    assert policy["report_only"] is True
    assert policy["opens_paper_trades"] is False
    assert policy["opens_live_orders"] is False
    assert policy["max_gate_status"] == "review_ready"
