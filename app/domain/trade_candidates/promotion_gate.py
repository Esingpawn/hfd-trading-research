from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from app.domain.trade_candidates.lifecycle import (
    PROMOTION_BLOCKER_ANTI_REPAINT_FAILED,
    PROMOTION_BLOCKER_ANTI_REPAINT_MISSING,
    PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN,
    PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED,
    PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING,
    PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED,
    PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING,
    PROMOTION_BLOCKER_SHADOW_MARKET_PAUSED,
    normalized_promotion_blockers,
)
from app.domain.whitelist_blacklist_policy import classify_setup_expectancy


GATE_STATUS_BLOCKED = "blocked"
GATE_STATUS_COLLECTING = "collecting"
GATE_STATUS_WATCHING_ENTRY = "watching_entry"
GATE_STATUS_REVIEW_READY = "review_ready"
GATE_STATUS_RETIRED = "retired"
PROMOTION_BLOCKER_RR_RATIO_BELOW_THRESHOLD = "rr_ratio_below_threshold"


@dataclass(frozen=True)
class PromotionGateEvidence:
    candidate_key: str
    lineage: str
    status: str
    promotion_status: str
    anti_repaint_status: str
    shadow_status: str
    promotion_blockers: tuple[str, ...] = ()
    entry_plan_state: dict[str, Any] | None = None
    shadow_stats: dict[str, Any] = field(default_factory=dict)
    setup_expectancy: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromotionGateDecision:
    candidate_key: str
    gate_status: str
    primary_blocker: str | None
    next_action: str
    blocker_groups: dict[str, list[dict[str, str]]]
    raw_blockers: list[str]
    evidence_summary: dict[str, Any]


def decide_promotion_gate(evidence: PromotionGateEvidence) -> PromotionGateDecision:
    raw_blockers = normalized_promotion_blockers(evidence.promotion_blockers)
    grouped = grouped_blockers(evidence, raw_blockers)
    gate_status = gate_status_for(evidence, grouped)
    primary = primary_blocker(grouped)
    return PromotionGateDecision(
        candidate_key=evidence.candidate_key,
        gate_status=gate_status,
        primary_blocker=primary,
        next_action=next_action_for(gate_status, primary),
        blocker_groups=grouped,
        raw_blockers=raw_blockers,
        evidence_summary=evidence_summary(evidence),
    )


def grouped_blockers(evidence: PromotionGateEvidence, raw_blockers: Iterable[str] | None = None) -> dict[str, list[dict[str, str]]]:
    blockers = list(raw_blockers if raw_blockers is not None else normalized_promotion_blockers(evidence.promotion_blockers))
    groups: dict[str, list[dict[str, str]]] = {}
    if evidence.lineage != "core_darkflow_v2":
        add_blocker(groups, "lineage", "non_core_darkflow_v2", "blocker", "该候选不属于 Core Darkflow v2，不能进入晋级闸门。")
    for blocker in blockers:
        category, severity, message = blocker_detail(blocker, evidence=evidence)
        add_blocker(groups, category, blocker, severity, message)

    entry_state = str((evidence.entry_plan_state or {}).get("state") or "")
    entry_reason = str((evidence.entry_plan_state or {}).get("reason") or "")
    if entry_state in {"waiting", "missing_price"}:
        add_blocker(groups, "entry_plan", f"entry_plan_{entry_state}", "waiting", entry_message(entry_state, entry_reason))
    elif entry_state in {"missed", "expired", "invalidated", "invalid_shape"}:
        add_blocker(groups, "entry_plan", f"entry_plan_{entry_state}", "blocker", entry_message(entry_state, entry_reason))

    if evidence.anti_repaint_status == "missing" and PROMOTION_BLOCKER_ANTI_REPAINT_MISSING not in blockers:
        add_blocker(groups, "anti_repaint", PROMOTION_BLOCKER_ANTI_REPAINT_MISSING, "blocker", "防重绘证据缺失，不能证明信号在决策当时可见。")
    if evidence.shadow_status == "not_started" and PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING not in blockers:
        add_blocker(groups, "shadow_forward", PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING, "waiting", "影子前向样本尚未开始积累。")
    setup_decision = setup_expectancy_decision(evidence)
    if setup_decision:
        classification = str(setup_decision.get("classification") or "")
        if classification in {"pause", "blacklist"}:
            blocker_code = "setup_expectancy_paused" if classification == "pause" else "setup_expectancy_blacklist"
            add_blocker(
                groups,
                "setup_expectancy",
                blocker_code,
                "blocker",
                _setup_expectancy_message(setup_decision),
            )
        elif classification == "collecting":
            add_blocker(
                groups,
                "setup_expectancy",
                "setup_expectancy_collecting",
                "waiting",
                _setup_expectancy_message(setup_decision),
            )
    return groups


def gate_status_for(evidence: PromotionGateEvidence, groups: dict[str, list[dict[str, str]]]) -> str:
    if evidence.promotion_status in {"entry_plan_retired", "duplicate_shadow_plan"} or evidence.shadow_status == "retired":
        return GATE_STATUS_RETIRED
    if any(item["severity"] == "blocker" for items in groups.values() for item in items):
        return GATE_STATUS_BLOCKED
    entry_state = str((evidence.entry_plan_state or {}).get("state") or "")
    if entry_state in {"waiting", "missing_price"}:
        return GATE_STATUS_WATCHING_ENTRY
    if any(item["severity"] == "waiting" for item in groups.get("setup_expectancy", [])):
        return GATE_STATUS_COLLECTING
    if evidence.promotion_status == "paper_review_ready":
        return GATE_STATUS_REVIEW_READY
    if evidence.shadow_status in {"collecting", "not_started"} or evidence.promotion_status in {"shadow_forward_collecting", "shadow_forward_pending"}:
        return GATE_STATUS_COLLECTING
    return GATE_STATUS_BLOCKED


def blocker_detail(code: str, *, evidence: PromotionGateEvidence | None = None) -> tuple[str, str, str]:
    rr_severity = "blocker" if evidence is not None and evidence.shadow_status == "passed" else "waiting"
    weak_quality_severity = "waiting"
    if evidence is not None and (evidence.status != "shadow_candidate" or evidence.shadow_status == "passed"):
        weak_quality_severity = "blocker"
    details = {
        PROMOTION_BLOCKER_ANTI_REPAINT_MISSING: ("anti_repaint", "blocker", "防重绘证据缺失，不能证明信号在决策当时可见。"),
        PROMOTION_BLOCKER_ANTI_REPAINT_FAILED: ("anti_repaint", "blocker", "防重绘审计失败，历史证据存在重绘风险。"),
        PROMOTION_BLOCKER_SHADOW_FORWARD_MISSING: ("shadow_forward", "waiting", "缺少 Core Darkflow v2 影子前向样本。"),
        PROMOTION_BLOCKER_SHADOW_FORWARD_COLLECTING: ("shadow_forward", "waiting", "Core Darkflow v2 影子前向样本仍在积累。"),
        PROMOTION_BLOCKER_SHADOW_FORWARD_FAILED: ("shadow_forward", "blocker", "影子前向样本未通过质量要求。"),
        PROMOTION_BLOCKER_ENTRY_PLAN_RETIRED: ("entry_plan", "blocker", "冻结入场计划已退休，不能继续推进。"),
        PROMOTION_BLOCKER_DUPLICATE_SHADOW_PLAN: ("dedupe", "blocker", "存在重复的影子前向计划，避免重复统计同一机会。"),
        PROMOTION_BLOCKER_SHADOW_MARKET_PAUSED: ("market_quality", "blocker", "该币种/方向的影子前向表现较弱，已暂停继续开新样本。"),
        PROMOTION_BLOCKER_RR_RATIO_BELOW_THRESHOLD: ("risk_shape", rr_severity, "盈亏比低于晋级阈值，可以继续积累影子样本，但不能进入纸上复核。"),
        "quality_score_below_threshold": ("risk_shape", weak_quality_severity, "质量评分低于晋级阈值，可以继续积累隔离影子样本，但不能进入纸上复核。"),
        "parent_trend_conflict": ("risk_shape", "blocker", "父级趋势与当前入场方向冲突，顺大势条件不成立。"),
        "body_break_invalidation": ("risk_shape", "blocker", "价格出现实体突破失效区，原暗流反应区已被破坏。"),
        "official_rule_unmapped": ("risk_shape", "blocker", "该信号尚未映射到明确教程规则，不能作为可信入场依据。"),
        "exit_filter_not_opening_playbook": ("risk_shape", "blocker", "该剧本只作为离场或过滤条件，不允许独立生成开仓候选。"),
    }
    return details.get(code, ("risk_shape", "blocker", f"存在未分类阻塞项：{code}"))


def add_blocker(groups: dict[str, list[dict[str, str]]], category: str, code: str, severity: str, message: str) -> None:
    items = groups.setdefault(category, [])
    if any(item["code"] == code for item in items):
        return
    items.append({"code": code, "severity": severity, "message": message})


def primary_blocker(groups: dict[str, list[dict[str, str]]]) -> str | None:
    priority = ["lineage", "anti_repaint", "entry_plan", "shadow_forward", "setup_expectancy", "market_quality", "dedupe", "risk_shape", "data_freshness"]
    for category in priority:
        for item in groups.get(category, []):
            if item["severity"] == "blocker":
                return item["code"]
    for category in priority:
        if groups.get(category):
            return groups[category][0]["code"]
    return None


def next_action_for(gate_status: str, primary: str | None) -> str:
    if gate_status == GATE_STATUS_REVIEW_READY:
        return "证据已接近完整，进入人工复核；第一版不自动标记为真实纸上可开。"
    if gate_status == GATE_STATUS_COLLECTING:
        return "继续积累 Core Darkflow v2 影子前向样本。"
    if gate_status == GATE_STATUS_WATCHING_ENTRY:
        return "继续观察冻结入场计划，等待价格触发或条件失效。"
    if gate_status == GATE_STATUS_RETIRED:
        return "候选已退休，不再推进；等待新的 Trade Candidate。"
    if primary == PROMOTION_BLOCKER_ANTI_REPAINT_MISSING:
        return "先生成或刷新 Anti-Repaint Evidence。"
    if primary == PROMOTION_BLOCKER_SHADOW_MARKET_PAUSED:
        return "暂停该市场方向的新样本，等待更多证据或策略复核。"
    if primary == "setup_expectancy_collecting":
        return "继续积累该教程玩法/市场状态的正期望样本，暂不进入真实纸上复核。"
    if primary in {"setup_expectancy_pause", "setup_expectancy_paused", "setup_expectancy_blacklist"}:
        return "该子组合正期望证据偏弱，暂停晋级；先复核规则或等待更多隔离样本。"
    return "处理主要阻塞项后重新刷新 Promotion Gate。"


def evidence_summary(evidence: PromotionGateEvidence) -> dict[str, Any]:
    entry_state = evidence.entry_plan_state or {}
    setup_decision = setup_expectancy_decision(evidence)
    return {
        "lineage": evidence.lineage,
        "candidate_status": evidence.status,
        "promotion_status": evidence.promotion_status,
        "anti_repaint_status": evidence.anti_repaint_status,
        "shadow_status": evidence.shadow_status,
        "entry_plan_state": entry_state.get("state"),
        "entry_plan_reason": entry_state.get("reason"),
        "shadow_closed_trades": evidence.shadow_stats.get("closed_trades"),
        "shadow_profit_factor": evidence.shadow_stats.get("profit_factor"),
        "shadow_win_rate": evidence.shadow_stats.get("win_rate"),
        "setup_expectancy": setup_expectancy_summary(evidence, setup_decision),
    }


def setup_expectancy_decision(evidence: PromotionGateEvidence) -> dict[str, Any] | None:
    if not evidence.setup_expectancy:
        return None
    return classify_setup_expectancy(evidence.setup_expectancy)


def setup_expectancy_summary(evidence: PromotionGateEvidence, decision: dict[str, Any] | None) -> dict[str, Any] | None:
    if not evidence.setup_expectancy:
        return None
    return {
        "classification": decision.get("classification") if decision else None,
        "display_text": decision.get("display_text") if decision else None,
        "reason_codes": decision.get("reason_codes") if decision else [],
        "reasons": decision.get("reasons") if decision else [],
        "evidence_source": evidence.setup_expectancy.get("evidence_source"),
        "sample_count": evidence.setup_expectancy.get("sample_count"),
        "closed_trades": evidence.setup_expectancy.get("closed_trades"),
        "invalid_outcome_trades": evidence.setup_expectancy.get("invalid_outcome_trades"),
        "win_rate": evidence.setup_expectancy.get("win_rate"),
        "profit_factor": evidence.setup_expectancy.get("profit_factor"),
        "avg_r_multiple": evidence.setup_expectancy.get("avg_r_multiple"),
        "median_r_multiple": evidence.setup_expectancy.get("median_r_multiple"),
        "max_drawdown": evidence.setup_expectancy.get("max_drawdown"),
        "time_exit_share": evidence.setup_expectancy.get("time_exit_share"),
    }


def _setup_expectancy_message(decision: dict[str, Any]) -> str:
    reasons = " ".join(str(item) for item in (decision.get("reasons") or []))
    display = str(decision.get("display_text") or "正期望证据")
    return f"正期望证据：{display}。{reasons}".strip()


def entry_message(state: str, reason: str) -> str:
    messages = {
        "waiting": "冻结入场计划仍在等待价格进入区间。",
        "missing_price": "缺少最新价格，暂时无法判断入场计划。",
        "missed": "价格已经越过冻结入场区间，本次入场计划错过。",
        "expired": "冻结入场计划已超过有效期。",
        "invalidated": "价格触发失效条件，冻结入场计划作废。",
        "invalid_shape": "入场、止损或目标形态不合法。",
    }
    base = messages.get(state, "入场计划存在阻塞。")
    return f"{base} 原因：{reason}" if reason else base


def promotion_gate_policy() -> dict[str, Any]:
    return {
        "lineage": "core_darkflow_v2",
        "excludes_legacy_control_research": True,
        "opens_paper_trades": False,
        "opens_live_orders": False,
        "report_only": True,
        "max_gate_status": GATE_STATUS_REVIEW_READY,
    }
