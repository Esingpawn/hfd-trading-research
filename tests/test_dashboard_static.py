from pathlib import Path

from fastapi.testclient import TestClient

from app.api.routers import dashboard as dashboard_router
from app import main as app_main


async def test_dashboard_serves_react_build_when_available(monkeypatch, tmp_path: Path) -> None:
    dist_html = tmp_path / "dist" / "index.html"
    legacy_html = tmp_path / "legacy" / "dashboard.html"
    dist_html.parent.mkdir()
    legacy_html.parent.mkdir()
    dist_html.write_text('<div id="root"></div><script src="/assets/index.js"></script>', encoding="utf-8")
    legacy_html.write_text('<main class="appShell">legacy</main>', encoding="utf-8")

    monkeypatch.setattr(dashboard_router, "DASHBOARD_DIST_HTML", dist_html)
    monkeypatch.setattr(dashboard_router, "DASHBOARD_HTML", legacy_html)

    response = await dashboard_router.dashboard()

    assert response.status_code == 200
    body = response.body.decode("utf-8")
    assert '<div id="root"></div>' in body
    assert 'src="/assets/' in body
    assert 'appShell' not in body


def test_dashboard_assets_are_mounted_when_build_exists(monkeypatch, tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    (assets_dir / "index.js").write_text("console.log('dashboard')", encoding="utf-8")

    monkeypatch.setattr(app_main, "DASHBOARD_DIST_ASSETS", assets_dir)

    response = TestClient(app_main.create_app()).get("/assets/index.js")

    assert response.status_code == 200
    assert "dashboard" in response.text


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


def test_dashboard_exposes_darkflow_interaction_report() -> None:
    html = Path("app/web/dashboard.html").read_text(encoding="utf-8")

    assert "darkflowInteractionBacktest" in html
    assert "/darkflow/interactions/backtest/latest" in html
    assert "darkflowInteractionSummary" in html
    assert "darkflowInteractions" in html


def test_dashboard_exposes_research_lineage_labels() -> None:
    html = Path("app/web/dashboard.html").read_text(encoding="utf-8")

    assert "lineageTag" in html
    assert "core_darkflow_v2" in html
    assert "legacy_feature_research" in html
    assert "Legacy/Control" in html


def test_react_dashboard_exposes_promotion_gate_chinese_read_model() -> None:
    source = Path("web/src/main.tsx").read_text(encoding="utf-8")

    assert "/darkflow/trade-candidates/promotion?limit=250" in source
    assert "gate_status_counts" in source
    assert "gate_samples" in source
    assert "gateStatusText" in source
    assert "待人工复核" in source
    assert "继续积累影子样本" in source
    assert "主要阻塞" in source
    assert "下一步" in source


def test_react_candidate_pool_summarizes_promotion_gate_counts() -> None:
    source = Path("web/src/main.tsx").read_text(encoding="utf-8")

    assert "晋级闸门分布" in source
    assert "晋级阻断" in source
    assert "观察入场区间" in source
    assert "候选已退休" in source


def test_react_dashboard_exposes_darkflow_alpha_scoreboard() -> None:
    source = Path("web/src/main.tsx").read_text(encoding="utf-8")

    assert "/darkflow/alpha-scoreboard?limit=50&min_closed_trades=1" in source
    assert "darkflow.alpha_accelerate" in source
    assert "暗流 Alpha 记分牌" in source
    assert "排队加速巡检" in source
    assert "只运行隔离影子前向路径" in source
    assert "可进入人工复核" in source
