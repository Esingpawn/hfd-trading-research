from __future__ import annotations

from typing import Any


STATUS_RANK = {"ok": 0, "warning": 1, "error": 2}


def build_diagnostics(
    runtime: dict[str, Any],
    completeness: dict[str, Any],
    telegram: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    actions: list[str] = []

    collector = _as_dict(runtime.get("collector"))
    paper_loop = _as_dict(runtime.get("paper_loop"))
    collection = _as_dict(runtime.get("collection"))
    latest_collection = _as_dict(collection.get("latest"))
    summary = _as_dict(completeness.get("summary"))
    scoring = _as_dict(summary.get("scoring"))
    research = _as_dict(summary.get("research"))

    if not collector.get("running"):
        _add_issue(
            issues,
            code="collector_not_running",
            severity="error",
            message="核心采集循环未运行。",
            action="运行 .\\scripts\\start-system.ps1 重启采集循环。",
        )

    last_error_line = _clean_text(collector.get("last_error_line"))
    if last_error_line:
        _add_issue(
            issues,
            code="collector_error_log",
            severity="warning",
            message="采集或服务错误日志有最新内容。",
            action=f"查看错误日志：{collector.get('stderr_log') or 'data/logs/*.err.log'}。",
            details={"last_error_line": last_error_line},
        )

    paper_last_error_line = _clean_text(paper_loop.get("last_error_line"))
    if paper_loop and not paper_loop.get("running"):
        _add_issue(
            issues,
            code="paper_loop_not_running",
            severity="warning",
            message="纸上交易循环未运行，样本不会自动积累。",
            action="运行 .\\scripts\\start-system.ps1 启动纸上交易循环。",
        )
    if paper_last_error_line:
        _add_issue(
            issues,
            code="paper_loop_error_log",
            severity="warning",
            message="纸上交易循环错误日志有最新内容。",
            action=f"查看错误日志：{paper_loop.get('stderr_log') or 'data/logs/paper-loop.err.log'}。",
            details={"last_error_line": paper_last_error_line},
        )

    if latest_collection:
        error_count = _as_int(latest_collection.get("error_count"))
        if error_count > 0:
            _add_issue(
                issues,
                code="collection_errors",
                severity="warning",
                message=f"最近采集完成但有 {error_count} 个错误。",
                action="查看 /system/runtime 的 last_error_line，并按需运行 .\\scripts\\update-data.ps1。",
            )
    else:
        _add_issue(
            issues,
            code="no_collection_run",
            severity="warning",
            message="尚未找到采集记录。",
            action="运行 .\\scripts\\update-data.ps1 补齐首批数据。",
        )

    scoring_missing = _as_int(scoring.get("missing_slots"))
    scoring_stale = _as_int(scoring.get("stale_slots"))
    if scoring_missing > 0:
        _add_issue(
            issues,
            code="scoring_missing",
            severity="error",
            message=f"评分核心数据缺失 {scoring_missing} 个槽位。",
            action="运行 .\\scripts\\update-data.ps1 或点击面板“补核心”。",
            details={"missing_slots": scoring_missing},
        )
    if scoring_stale > 0:
        _add_issue(
            issues,
            code="scoring_stale",
            severity="error",
            message=f"评分核心数据过期 {scoring_stale} 个槽位。",
            action="运行 .\\scripts\\update-data.ps1 或等待采集循环完成下一轮。",
            details={"stale_slots": scoring_stale},
        )

    research_missing = _as_int(research.get("missing_slots"))
    research_stale = _as_int(research.get("stale_slots"))
    if research_missing > 0 or research_stale > 0:
        _add_issue(
            issues,
            code="research_incomplete",
            severity="warning",
            message=f"全量研究数据缺失 {research_missing} 个、过期 {research_stale} 个槽位。",
            action="需要复盘解释或调参前，运行 .\\scripts\\update-data.ps1 补全。",
            details={"missing_slots": research_missing, "stale_slots": research_stale},
        )

    telegram_payload = _as_dict(telegram)
    if telegram_payload.get("error"):
        _add_issue(
            issues,
            code="telegram_error",
            severity="warning",
            message="Telegram 状态检查异常。",
            action="检查 .env 中的 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID。",
            details={"error": str(telegram_payload.get("error"))},
        )
    elif telegram_payload and not telegram_payload.get("configured"):
        _add_issue(
            issues,
            code="telegram_not_configured",
            severity="warning",
            message="Telegram 未配置，纸上交易通知不会发送。",
            action="需要通知时，按 README 配置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID。",
        )

    for issue in issues:
        action = issue.get("action")
        if isinstance(action, str) and action and action not in actions:
            actions.append(action)

    overall_status = _overall_status(issues)
    return {
        "overall_status": overall_status,
        "label": {"ok": "正常", "warning": "需关注", "error": "异常"}[overall_status],
        "summary": _summary_text(overall_status, issues),
        "issues": issues,
        "actions": actions,
        "metrics": {
            "collector_running": bool(collector.get("running")),
            "paper_loop_running": bool(paper_loop.get("running")),
            "collector_age_seconds": _as_float(latest_collection.get("age_seconds")),
            "scoring_missing_slots": scoring_missing,
            "scoring_stale_slots": scoring_stale,
            "research_missing_slots": research_missing,
            "research_stale_slots": research_stale,
            "last_error_line": last_error_line,
        },
    }


def _add_issue(
    issues: list[dict[str, Any]],
    *,
    code: str,
    severity: str,
    message: str,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    issues.append(
        {
            "code": code,
            "severity": severity,
            "message": message,
            "action": action,
            "details": details or {},
        }
    )


def _overall_status(issues: list[dict[str, Any]]) -> str:
    status = "ok"
    for issue in issues:
        severity = str(issue.get("severity") or "warning")
        if STATUS_RANK.get(severity, 1) > STATUS_RANK[status]:
            status = severity if severity in STATUS_RANK else "warning"
    return status


def _summary_text(status: str, issues: list[dict[str, Any]]) -> str:
    if status == "ok":
        return "服务、采集循环和评分核心数据均正常。"
    primary_issue = max(
        issues,
        key=lambda issue: STATUS_RANK.get(str(issue.get("severity") or "warning"), 1),
        default=None,
    )
    first = primary_issue["message"] if primary_issue else "存在需要处理的诊断项。"
    if len(issues) == 1:
        return first
    return f"{first} 另有 {len(issues) - 1} 项需要关注。"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
