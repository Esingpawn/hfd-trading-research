from __future__ import annotations

from fastapi import APIRouter, Query

from app.services.telegram import TelegramClient, extract_chat_candidates

router = APIRouter()


@router.get("/telegram/status")
async def telegram_status() -> dict[str, object]:
    status = await TelegramClient().status()
    return status.__dict__


@router.get("/telegram/updates")
async def telegram_updates(limit: int = Query(default=10, ge=1, le=100)) -> dict[str, object]:
    updates = await TelegramClient().get_updates(limit=limit)
    return {"chats": extract_chat_candidates(updates), "update_count": len(updates)}


@router.post("/telegram/send")
async def telegram_send(
    text: str = Query(..., min_length=1),
    chat_id: str | None = Query(default=None),
) -> dict[str, object]:
    result = await TelegramClient().send_message(text, chat_id=chat_id)
    return {"message_id": result.get("message_id"), "date": result.get("date")}
