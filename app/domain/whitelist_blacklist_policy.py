from __future__ import annotations

from typing import Any


CORE_EVIDENCE_SOURCES = {"shadow_forward", "paper"}


DEFAULT_POLICY = {
    "whitelist_min_closed": 5,
    "pause_min_closed": 3,
    "whitelist_min_win_rate": 0.60,
    "whitelist_min_profit_factor": 1.50,
    "whitelist_max_drawdown": 0.08,
    "pause_max_win_rate": 0.35,
    "pause_max_profit_factor": 0.85,
    "blacklist_max_profit_factor": 0.50,
    "blacklist_max_win_rate": 0.25,
    "blacklist_secondary_max_profit_factor": 0.75,
    "time_exit_problem_share": 0.70,
    "time_exit_problem_profit_factor": 1.05,
    "drawdown_problem": 0.12,
    "drawdown_problem_profit_factor": 1.10,
    "invalid_outcome_ratio_pause": 0.30,
    "strategy_min_closed": 10,
    "strategy_keep_min_win_rate": 0.55,
    "strategy_keep_min_profit_factor": 1.25,
    "strategy_keep_max_drawdown": 0.12,
    "strategy_deweight_max_time_exit_share": 0.65,
}


DISPLAY_TEXT = {
    "whitelist": "白名单补样",
    "collecting": "继续补样",
    "review_ready": "可人工复核",
    "observe": "继续观察",
    "pause": "暂停补样",
    "blacklist": "黑名单隔离",
}

SAMPLING_ACTION = {
    "whitelist": "prioritize",
    "review_ready": "prioritize",
    "collecting": "prioritize",
    "observe": "watch",
    "pause": "pause",
    "blacklist": "block",
}

REASONS_ZH = {
    "positive_expectancy_passed": "胜率、盈利因子和回撤同时达标。",
    "time_exit_share_healthy": "时间退出占比没有异常升高，说明不是靠拖到结束维持结果。",
    "no_closed_samples": "还没有平仓样本，继续只在影子环境补样。",
    "insufficient_closed_samples": "平仓样本不足，尚不能进入白名单或黑名单。",
    "non_core_darkflow_evidence": "该证据不是 Core Darkflow v2 前向/纸上来源，不能用于白名单晋级。",
    "invalid_outcome_ratio_high": "无效结果占比过高，先修正数据质量再判断期望。",
    "weak_edge": "当前前向胜率或盈利因子偏弱。",
    "severe_edge": "前向胜率或盈利因子已经明显低于保留门槛。",
    "time_exit_problem": "大量持仓拖到时间退出，说明入场或止盈逻辑没有形成有效边际。",
    "drawdown_problem": "回撤压力超过保守观察线。",
    "unclear_edge": "样本已有一定数量，但胜率、盈利因子或回撤没有同时给出明确方向。",
    "strategy_samples_collecting": "策略级样本不足，先继续隔离补样。",
    "strategy_expectancy_kept": "策略级前向胜率、盈利因子和回撤达到主路径保留线。",
    "strategy_expectancy_weak": "策略级前向结果不足以继续占用主路径权重。",
    "strategy_time_exit_weak": "时间退出占比过高，说明该玩法在当前实现中缺少有效退出优势。",
    "strategy_review_needed": "策略级表现不够强，也没有弱到需要整体移出，保持人工复核。",
}


def classify_setup_expectancy(row: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    thresholds = DEFAULT_POLICY | dict(policy or {})
    closed = int(row.get("sample_count") or row.get("valid_outcome_trades") or 0)
    closed_all = int(row.get("closed_trades") or closed)
    invalid = int(row.get("invalid_outcome_trades") or 0)
    win_rate = _number(row.get("win_rate"))
    profit_factor = _number(row.get("profit_factor"))
    max_drawdown = _number(row.get("max_drawdown"))
    time_exit_share = _number(row.get("time_exit_share"))
    evidence_source = str(row.get("evidence_source") or "")
    reason_codes: list[str] = []

    if evidence_source not in CORE_EVIDENCE_SOURCES:
        reason_codes.append("non_core_darkflow_evidence")
        return _decision("collecting", reason_codes, can_promote=False)

    invalid_ratio = invalid / closed_all if closed_all > 0 else 0.0
    if invalid_ratio >= float(thresholds["invalid_outcome_ratio_pause"]):
        reason_codes.append("invalid_outcome_ratio_high")
        return _decision("pause", reason_codes, can_promote=False)

    if closed <= 0:
        reason_codes.append("no_closed_samples")
        return _decision("collecting", reason_codes, can_promote=False)

    if (
        closed >= int(thresholds["whitelist_min_closed"])
        and _at_least(win_rate, thresholds["whitelist_min_win_rate"])
        and _at_least(profit_factor, thresholds["whitelist_min_profit_factor"])
        and _at_most(max_drawdown, thresholds["whitelist_max_drawdown"])
        and (time_exit_share is None or time_exit_share <= float(thresholds["strategy_deweight_max_time_exit_share"]) - 0.10)
    ):
        reason_codes.extend(["positive_expectancy_passed", "time_exit_share_healthy"])
        return _decision("whitelist", reason_codes, can_promote=True)

    if closed >= int(thresholds["pause_min_closed"]):
        weak_edge = _at_most(win_rate, thresholds["pause_max_win_rate"]) and _at_most(profit_factor, thresholds["pause_max_profit_factor"])
        severe_edge = _at_most(profit_factor, thresholds["blacklist_max_profit_factor"]) or (
            _at_most(win_rate, thresholds["blacklist_max_win_rate"])
            and _at_most(profit_factor, thresholds["blacklist_secondary_max_profit_factor"])
        )
        time_exit_problem = (
            time_exit_share is not None
            and time_exit_share >= float(thresholds["time_exit_problem_share"])
            and (profit_factor is None or profit_factor < float(thresholds["time_exit_problem_profit_factor"]))
        )
        drawdown_problem = max_drawdown is not None and max_drawdown > float(thresholds["drawdown_problem"])
        if severe_edge or (closed >= int(thresholds["whitelist_min_closed"]) and weak_edge and time_exit_problem):
            reason_codes.append("severe_edge")
            if time_exit_problem:
                reason_codes.append("time_exit_problem")
            return _decision("blacklist", reason_codes, can_promote=False)
        if weak_edge or time_exit_problem or drawdown_problem:
            reason_codes.append("weak_edge")
            if time_exit_problem:
                reason_codes.append("time_exit_problem")
            if drawdown_problem:
                reason_codes.append("drawdown_problem")
            return _decision("pause", reason_codes, can_promote=False)

    if closed < int(thresholds["whitelist_min_closed"]):
        reason_codes.append("insufficient_closed_samples")
        return _decision("collecting", reason_codes, can_promote=False)

    reason_codes.append("unclear_edge")
    return _decision("observe", reason_codes, can_promote=False)


def strategy_action_from_expectancy(row: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    thresholds = DEFAULT_POLICY | dict(policy or {})
    closed = int(row.get("sample_count") or row.get("valid_outcome_trades") or row.get("closed_trades") or 0)
    win_rate = _number(row.get("win_rate"))
    profit_factor = _number(row.get("profit_factor"))
    max_drawdown = _number(row.get("max_drawdown"))
    time_exit_share = _number(row.get("time_exit_share"))
    if closed < int(thresholds["strategy_min_closed"]):
        return _strategy_action("collect_more", 1.0, ["strategy_samples_collecting"])
    if (
        _at_least(win_rate, thresholds["strategy_keep_min_win_rate"])
        and _at_least(profit_factor, thresholds["strategy_keep_min_profit_factor"])
        and _at_most(max_drawdown, thresholds["strategy_keep_max_drawdown"])
    ):
        return _strategy_action("keep", 1.0, ["strategy_expectancy_kept"])
    if (
        (profit_factor is not None and profit_factor < 1.0)
        or (win_rate is not None and win_rate < 0.45)
        or (time_exit_share is not None and time_exit_share >= float(thresholds["strategy_deweight_max_time_exit_share"]))
    ):
        reasons = ["strategy_expectancy_weak"]
        if time_exit_share is not None and time_exit_share >= float(thresholds["strategy_deweight_max_time_exit_share"]):
            reasons.append("strategy_time_exit_weak")
        return _strategy_action("deweight", 0.35, reasons)
    return _strategy_action("review", 0.75, ["strategy_review_needed"])


def _decision(classification: str, reason_codes: list[str], *, can_promote: bool) -> dict[str, Any]:
    return {
        "classification": classification,
        "display_text": DISPLAY_TEXT[classification],
        "sampling_action": SAMPLING_ACTION[classification],
        "can_promote": can_promote,
        "reason_codes": reason_codes,
        "reasons": [REASONS_ZH[code] for code in reason_codes],
    }


def _strategy_action(action: str, weight_multiplier: float, reason_codes: list[str]) -> dict[str, Any]:
    return {
        "main_path_action": action,
        "action_text": {
            "keep": "主路径保留",
            "collect_more": "继续补样",
            "review": "人工复核",
            "deweight": "主路径降权",
        }[action],
        "weight_multiplier": weight_multiplier,
        "reason_codes": reason_codes,
        "reasons": [REASONS_ZH[code] for code in reason_codes],
    }


def _at_least(value: float | None, threshold: Any) -> bool:
    return value is not None and value >= float(threshold)


def _at_most(value: float | None, threshold: Any) -> bool:
    return value is not None and value <= float(threshold)


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed == parsed else None
    return None
