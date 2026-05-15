from pathlib import Path


def test_dashboard_exposes_background_research_refresh() -> None:
    html = Path("app/web/dashboard.html").read_text(encoding="utf-8")

    assert "queueResearchReportRefresh" in html
    assert "/features/research-reports/refresh" in html
    assert "force=true" in html
    assert "researchReportMeta" in html
    assert "researchLimitMeta" in html


def test_dashboard_exposes_shadow_promotion_and_cost_model() -> None:
    html = Path("app/web/dashboard.html").read_text(encoding="utf-8")

    assert "shadowPromotion" in html
    assert "uses_fee_and_slippage" in html
    assert "promotion_status" in html
    assert "row.horizon" in html
    assert "shadowPaperHorizonStats" in html
    assert "edge_unstable_drawdown" in html
    assert "最大回撤" in html
