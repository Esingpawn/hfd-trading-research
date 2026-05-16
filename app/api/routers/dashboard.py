from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from app.api.shared import DASHBOARD_DIST_HTML, DASHBOARD_HTML

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> Response:
    html_path = DASHBOARD_DIST_HTML if DASHBOARD_DIST_HTML.exists() else DASHBOARD_HTML
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
