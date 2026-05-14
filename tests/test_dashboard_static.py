from pathlib import Path


def test_dashboard_exposes_background_research_refresh() -> None:
    html = Path("app/web/dashboard.html").read_text(encoding="utf-8")

    assert "queueResearchReportRefresh" in html
    assert "/features/research-reports/refresh" in html
    assert "researchReportMeta" in html
    assert "researchLimitMeta" in html
