from fastapi.testclient import TestClient

from app.api.deps import get_session
from app.main import app, create_app


def test_task_enqueue_exposes_research_quality_params() -> None:
    route = next(route for route in app.routes if getattr(route, "path", None) == "/tasks/enqueue")
    dependant = getattr(route, "dependant", None)
    query_params = {param.name for param in dependant.query_params}

    assert "dedupe_research_samples" in query_params
    assert "min_unique_event_days" in query_params
    assert "min_unique_collection_runs" in query_params
    assert "replay_limit" in query_params
    assert "shadow_limit" in query_params
    assert "scoreboard_limit" in query_params
    assert "min_closed_trades" in query_params
    assert "max_candidate_age_hours" in query_params
    assert "entry_tolerance_pct" in query_params
    assert "materialize" in query_params
    assert "mark_first" in query_params
    assert "force" in query_params
    assert "confirmation_window_minutes" in query_params


def _task_enqueue_client(monkeypatch) -> TestClient:
    async def fake_session():
        yield object()

    async def fake_enqueue_task(session, *, task_name: str, payload: dict | None = None):
        return {
            "task_run_id": "test-task-run",
            "status": "recorded",
            "task_name": task_name,
            "payload": payload or {},
        }

    monkeypatch.setattr("app.api.routers.tasks.enqueue_task", fake_enqueue_task)
    test_app = create_app()
    test_app.dependency_overrides[get_session] = fake_session
    return TestClient(test_app)


def test_task_enqueue_rejects_unknown_task_without_force(monkeypatch) -> None:
    client = _task_enqueue_client(monkeypatch)

    response = client.post("/tasks/enqueue", params={"task_name": "one_off.cleanup"})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unknown_task_requires_force"


def test_task_enqueue_rejects_legacy_research_task_without_force(monkeypatch) -> None:
    client = _task_enqueue_client(monkeypatch)

    response = client.post("/tasks/enqueue", params={"task_name": "features.research_reports"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "non_production_task_requires_force"
    assert detail["lineage"] == "legacy_feature_research"
    assert detail["heavy"] is True


def test_task_enqueue_allows_legacy_research_task_with_force(monkeypatch) -> None:
    client = _task_enqueue_client(monkeypatch)

    response = client.post("/tasks/enqueue", params={"task_name": "features.research_reports", "force": "true"})

    assert response.status_code == 200
    body = response.json()
    assert body["task_name"] == "features.research_reports"
    assert body["payload"]["force"] is True


def test_task_enqueue_allows_core_darkflow_task_without_force(monkeypatch) -> None:
    client = _task_enqueue_client(monkeypatch)

    response = client.post("/tasks/enqueue", params={"task_name": "darkflow.alpha_accelerate"})

    assert response.status_code == 200
    assert response.json()["task_name"] == "darkflow.alpha_accelerate"


def test_expected_api_routes_are_registered() -> None:
    expected = {
        ("GET", "/health"),
        ("GET", "/system/runtime"),
        ("GET", "/system/storage"),
        ("GET", "/data/completeness"),
        ("GET", "/data/quality-report"),
        ("GET", "/market/overview"),
        ("POST", "/collect/run"),
        ("POST", "/collect/prices"),
        ("POST", "/paper/scan"),
        ("GET", "/paper/trades"),
        ("POST", "/shadow-paper/scan"),
        ("POST", "/shadow-paper/mark"),
        ("POST", "/shadow-paper/replay"),
        ("POST", "/shadow-paper/replay-all"),
        ("GET", "/shadow-paper/trades"),
        ("GET", "/shadow-paper/stats"),
        ("GET", "/shadow-paper/darkflow-playbook-attribution"),
        ("GET", "/shadow-paper/darkflow-subportfolio-recommendations"),
        ("GET", "/shadow-paper/darkflow-setup-expectancy"),
        ("GET", "/shadow-paper/darkflow-trend-extension-exit"),
        ("GET", "/shadow-paper/darkflow-time-exit-review"),
        ("POST", "/signals/backfill"),
        ("GET", "/signals/weights"),
        ("GET", "/darkflow/rulebook"),
        ("GET", "/darkflow/playbooks"),
        ("GET", "/darkflow/playbooks/backtest"),
        ("GET", "/darkflow/playbooks/backtest/latest"),
        ("POST", "/darkflow/playbooks/backtest"),
        ("POST", "/darkflow/zones/backfill"),
        ("POST", "/darkflow/interactions/backfill"),
        ("GET", "/darkflow/decision-cards"),
        ("GET", "/darkflow/trade-candidates"),
        ("POST", "/darkflow/trade-candidates/materialize"),
        ("GET", "/darkflow/trade-candidates/promotion"),
        ("GET", "/darkflow/trade-candidates/entry-plan-states"),
        ("GET", "/darkflow/trade-candidates/waiting"),
        ("POST", "/darkflow/trade-candidates/audit"),
        ("POST", "/darkflow/trade-candidates/shadow-forward"),
        ("POST", "/darkflow/trade-candidates/promotion/refresh"),
        ("GET", "/darkflow/alpha-scoreboard"),
        ("POST", "/darkflow/alpha/accelerate"),
        ("GET", "/darkflow/interactions/backtest"),
        ("GET", "/darkflow/interactions/backtest/latest"),
        ("POST", "/darkflow/interactions/backtest"),
        ("POST", "/darkflow/shadow-replay"),
        ("POST", "/features/backfill"),
        ("POST", "/features/labels/backfill"),
        ("POST", "/features/reset"),
        ("POST", "/features/refresh"),
        ("POST", "/features/research-reports/refresh"),
        ("GET", "/features/effectiveness"),
        ("GET", "/features/candidates"),
        ("GET", "/features/candidates/latest"),
        ("POST", "/features/candidates"),
        ("GET", "/features/paper-ab"),
        ("GET", "/features/paper-ab/latest"),
        ("POST", "/features/paper-ab"),
        ("GET", "/features/segment-candidates"),
        ("GET", "/features/segment-candidates/latest"),
        ("POST", "/features/segment-candidates"),
        ("GET", "/features/segment-paper-ab"),
        ("GET", "/features/segment-paper-ab/latest"),
        ("POST", "/features/segment-paper-ab"),
        ("GET", "/governance/experiments"),
        ("POST", "/governance/experiments"),
        ("GET", "/governance/weights"),
        ("POST", "/governance/weights"),
        ("POST", "/backtests/batch"),
        ("GET", "/tasks"),
        ("POST", "/tasks/enqueue"),
        ("GET", "/trading/safety"),
        ("PATCH", "/trading/safety"),
        ("GET", "/trading/orders"),
        ("POST", "/trading/orders"),
        ("GET", "/trading/audit"),
        ("GET", "/telegram/status"),
        ("GET", "/dashboard"),
    }
    registered = {
        (method, route.path)
        for route in app.routes
        for method in (getattr(route, "methods", None) or [])
    }

    assert expected.issubset(registered)
