from app.main import app


def test_task_enqueue_exposes_research_quality_params() -> None:
    route = next(route for route in app.routes if getattr(route, "path", None) == "/tasks/enqueue")
    dependant = getattr(route, "dependant", None)
    query_params = {param.name for param in dependant.query_params}

    assert "dedupe_research_samples" in query_params
    assert "min_unique_event_days" in query_params
    assert "min_unique_collection_runs" in query_params
    assert "replay_limit" in query_params
    assert "force" in query_params
    assert "confirmation_window_minutes" in query_params


def test_expected_api_routes_are_registered() -> None:
    expected = {
        ("GET", "/health"),
        ("GET", "/system/runtime"),
        ("GET", "/system/storage"),
        ("GET", "/data/completeness"),
        ("GET", "/data/quality-report"),
        ("GET", "/market/overview"),
        ("POST", "/collect/run"),
        ("POST", "/paper/scan"),
        ("GET", "/paper/trades"),
        ("POST", "/shadow-paper/scan"),
        ("POST", "/shadow-paper/mark"),
        ("POST", "/shadow-paper/replay"),
        ("POST", "/shadow-paper/replay-all"),
        ("GET", "/shadow-paper/trades"),
        ("GET", "/shadow-paper/stats"),
        ("POST", "/signals/backfill"),
        ("GET", "/signals/weights"),
        ("GET", "/darkflow/rulebook"),
        ("GET", "/darkflow/playbooks"),
        ("GET", "/darkflow/playbooks/backtest"),
        ("GET", "/darkflow/playbooks/backtest/latest"),
        ("POST", "/darkflow/playbooks/backtest"),
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
