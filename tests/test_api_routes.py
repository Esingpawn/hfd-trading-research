from app.main import app


def test_expected_api_routes_are_registered() -> None:
    expected = {
        ("GET", "/health"),
        ("GET", "/system/runtime"),
        ("GET", "/system/storage"),
        ("GET", "/data/completeness"),
        ("GET", "/market/overview"),
        ("POST", "/collect/run"),
        ("POST", "/paper/scan"),
        ("GET", "/paper/trades"),
        ("POST", "/signals/backfill"),
        ("GET", "/signals/weights"),
        ("POST", "/features/backfill"),
        ("POST", "/features/labels/backfill"),
        ("POST", "/features/refresh"),
        ("GET", "/features/effectiveness"),
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
