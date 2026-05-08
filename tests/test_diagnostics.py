from app.services.diagnostics import build_diagnostics


def runtime_payload(
    running: bool = True,
    latest: dict | None = None,
    last_error: str | None = None,
    paper_running: bool | None = None,
    paper_last_error: str | None = None,
) -> dict:
    payload = {
        "collector": {
            "running": running,
            "stderr_log": "data/logs/collect-core-loop.err.log",
            "last_error_line": last_error,
        },
        "collection": {
            "latest": latest
            if latest is not None
            else {"error_count": 0, "age_seconds": 120.0, "finished_at": "2026-05-07T10:00:00Z"}
        },
    }
    if paper_running is not None or paper_last_error is not None:
        payload["paper_loop"] = {
            "running": bool(paper_running),
            "stderr_log": "data/logs/paper-loop.err.log",
            "last_error_line": paper_last_error,
        }
    return payload


def completeness_payload(scoring_missing: int = 0, scoring_stale: int = 0, research_missing: int = 0, research_stale: int = 0) -> dict:
    return {
        "summary": {
            "scoring": {"missing_slots": scoring_missing, "stale_slots": scoring_stale},
            "research": {"missing_slots": research_missing, "stale_slots": research_stale},
        }
    }


def test_diagnostics_ok_when_collector_and_scoring_are_ready() -> None:
    result = build_diagnostics(
        runtime_payload(),
        completeness_payload(),
        {"configured": True, "error": None},
    )

    assert result["overall_status"] == "ok"
    assert result["issues"] == []


def test_diagnostics_errors_when_collector_is_not_running() -> None:
    result = build_diagnostics(runtime_payload(running=False), completeness_payload())

    assert result["overall_status"] == "error"
    assert result["issues"][0]["code"] == "collector_not_running"
    assert result["actions"]


def test_diagnostics_errors_when_scoring_data_is_missing_or_stale() -> None:
    result = build_diagnostics(
        runtime_payload(),
        completeness_payload(scoring_missing=2, scoring_stale=3),
    )

    codes = {issue["code"] for issue in result["issues"]}
    assert result["overall_status"] == "error"
    assert {"scoring_missing", "scoring_stale"}.issubset(codes)
    assert result["metrics"]["scoring_missing_slots"] == 2
    assert result["metrics"]["scoring_stale_slots"] == 3


def test_diagnostics_warns_for_research_and_telegram_without_blocking_core() -> None:
    result = build_diagnostics(
        runtime_payload(),
        completeness_payload(research_missing=4, research_stale=5),
        {"configured": False, "error": None},
    )

    codes = {issue["code"] for issue in result["issues"]}
    assert result["overall_status"] == "warning"
    assert {"research_incomplete", "telegram_not_configured"}.issubset(codes)


def test_diagnostics_surfaces_last_error_line() -> None:
    result = build_diagnostics(
        runtime_payload(last_error="boom"),
        completeness_payload(),
    )

    assert result["overall_status"] == "warning"
    assert result["metrics"]["last_error_line"] == "boom"
    assert result["issues"][0]["details"]["last_error_line"] == "boom"


def test_diagnostics_warns_when_paper_loop_is_not_running() -> None:
    result = build_diagnostics(
        runtime_payload(paper_running=False),
        completeness_payload(),
    )

    codes = {issue["code"] for issue in result["issues"]}
    assert result["overall_status"] == "warning"
    assert "paper_loop_not_running" in codes
    assert result["metrics"]["paper_loop_running"] is False


def test_diagnostics_surfaces_paper_loop_error_line() -> None:
    result = build_diagnostics(
        runtime_payload(paper_running=True, paper_last_error="paper boom"),
        completeness_payload(),
    )

    codes = {issue["code"] for issue in result["issues"]}
    assert result["overall_status"] == "warning"
    assert "paper_loop_error_log" in codes


def test_diagnostics_warns_when_collection_has_not_run() -> None:
    result = build_diagnostics(
        runtime_payload(latest={}),
        completeness_payload(),
    )

    assert result["overall_status"] == "warning"
    assert result["issues"][0]["code"] == "no_collection_run"


def test_diagnostics_summary_prefers_errors_over_warnings() -> None:
    result = build_diagnostics(
        runtime_payload(last_error="network hiccup"),
        completeness_payload(scoring_missing=1),
    )

    assert result["overall_status"] == "error"
    assert "评分核心数据缺失" in result["summary"]
