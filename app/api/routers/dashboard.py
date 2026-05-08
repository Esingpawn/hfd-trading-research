from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from app.api.shared import DASHBOARD_HTML

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> Response:
    return HTMLResponse(
        DASHBOARD_HTML.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
