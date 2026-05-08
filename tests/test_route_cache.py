from app.api import routes


def test_completeness_cache_is_invalidated_by_collection_token() -> None:
    routes._completeness_cache_clear()

    routes._completeness_cache_set({"summary": {"version": 1}}, "run-a")

    assert routes._completeness_cache_get("run-a") == {"summary": {"version": 1}}
    assert routes._completeness_cache_get("run-b") is None

    routes._completeness_cache_clear()


def test_market_cache_is_invalidated_by_collection_token() -> None:
    routes._market_cache_clear()

    routes._market_cache_set([{"symbol": "BTCUSDT"}], "run-a")

    assert routes._market_cache_get("run-a") == [{"symbol": "BTCUSDT"}]
    assert routes._market_cache_get("run-b") is None

    routes._market_cache_clear()
