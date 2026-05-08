from __future__ import annotations

from app.api.routers import build_api_router
from app.api.shared import (
    _completeness_cache_clear,
    _completeness_cache_get,
    _completeness_cache_set,
    _market_cache_clear,
    _market_cache_get,
    _market_cache_set,
)

router = build_api_router()

__all__ = [
    "router",
    "_completeness_cache_clear",
    "_completeness_cache_get",
    "_completeness_cache_set",
    "_market_cache_clear",
    "_market_cache_get",
    "_market_cache_set",
]
