from __future__ import annotations

from fastapi import APIRouter

from app.api.routers import backtests, collection, dashboard, governance, market, paper, signals, system, tasks, telegram


def build_api_router() -> APIRouter:
    router = APIRouter()
    router.include_router(system.router)
    router.include_router(market.router)
    router.include_router(collection.router)
    router.include_router(paper.router)
    router.include_router(signals.router)
    router.include_router(governance.router)
    router.include_router(backtests.router)
    router.include_router(tasks.router)
    router.include_router(telegram.router)
    router.include_router(dashboard.router)
    return router
