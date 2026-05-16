from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.api.shared import DASHBOARD_DIST_ASSETS


def create_app() -> FastAPI:
    app = FastAPI(title="HFD Trading Research", version="0.1.0")
    if DASHBOARD_DIST_ASSETS.exists():
        app.mount("/assets", StaticFiles(directory=DASHBOARD_DIST_ASSETS), name="dashboard-assets")
    app.include_router(router)
    return app


app = create_app()
