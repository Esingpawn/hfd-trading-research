from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings


@dataclass(frozen=True)
class TelegramStatus:
    configured: bool
    has_chat_id: bool
    bot_username: str | None = None
    relay_configured: bool = False
    error: str | None = None


class TelegramClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.token = self.settings.telegram_bot_token
        self.chat_id = self.settings.telegram_chat_id
        self.relay_url = self.settings.telegram_relay_url
        self.relay_secret = self.settings.telegram_relay_secret

    @property
    def configured(self) -> bool:
        if self.relay_url:
            return bool(self.relay_secret)
        return bool(self.token)

    async def get_me(self, timeout: float | None = None) -> dict[str, Any]:
        self._require_token()
        async with httpx.AsyncClient(
            timeout=timeout or self.settings.http_timeout_seconds
        ) as client:
            if self.relay_url:
                response = await client.get(
                    self._relay_url("getMe"), headers=self._relay_headers()
                )
            else:
                response = await client.get(self._url("getMe"))
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok"):
            raise ValueError(f"Telegram getMe failed: {payload}")
        return payload["result"]

    async def get_updates(self, limit: int = 10) -> list[dict[str, Any]]:
        self._require_token()
        async with httpx.AsyncClient(timeout=self.settings.http_timeout_seconds) as client:
            if self.relay_url:
                response = await client.get(
                    self._relay_url("getUpdates"),
                    params={"limit": limit},
                    headers=self._relay_headers(),
                )
            else:
                response = await client.get(self._url("getUpdates"), params={"limit": limit})
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok"):
            raise ValueError(f"Telegram getUpdates failed: {payload}")
        return payload["result"]

    async def send_message(self, text: str, chat_id: str | None = None) -> dict[str, Any]:
        self._require_token()
        target_chat_id = chat_id or self.chat_id
        if not target_chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is not configured")
        payload = {
            "chat_id": target_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        async with httpx.AsyncClient(timeout=self.settings.http_timeout_seconds) as client:
            if self.relay_url:
                response = await client.post(
                    self._relay_url("sendMessage"),
                    json=payload,
                    headers=self._relay_headers(),
                )
            else:
                response = await client.post(self._url("sendMessage"), json=payload)
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok"):
            raise ValueError(f"Telegram sendMessage failed: {payload}")
        return payload["result"]

    async def status(self) -> TelegramStatus:
        cached = _status_cache_get()
        if cached is not None:
            return cached
        if not self.configured:
            status = TelegramStatus(
                configured=False,
                has_chat_id=False,
                relay_configured=bool(self.relay_url),
            )
            _status_cache_set(status)
            return status
        try:
            me = await self.get_me(timeout=min(self.settings.http_timeout_seconds, 5.0))
            status = TelegramStatus(
                configured=True,
                has_chat_id=bool(self.chat_id),
                bot_username=me.get("username"),
                relay_configured=bool(self.relay_url),
            )
            _status_cache_set(status)
            return status
        except Exception as exc:  # noqa: BLE001
            status = TelegramStatus(
                configured=True,
                has_chat_id=bool(self.chat_id),
                relay_configured=bool(self.relay_url),
                error=str(exc) or exc.__class__.__name__,
            )
            _status_cache_set(status)
            return status

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _relay_url(self, method: str) -> str:
        return f"{self.relay_url}/telegram/{method}"

    def _relay_headers(self) -> dict[str, str]:
        return {"X-HFD-Relay-Secret": self.relay_secret}

    def _require_token(self) -> None:
        if self.relay_url and not self.relay_secret:
            raise ValueError("TELEGRAM_RELAY_SECRET is not configured")
        if not self.relay_url and not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured")


def extract_chat_candidates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        key = str(chat_id)
        candidates[key] = {
            "chat_id": key,
            "type": chat.get("type"),
            "title": chat.get("title"),
            "username": chat.get("username"),
            "first_name": chat.get("first_name"),
            "last_name": chat.get("last_name"),
            "last_text": message.get("text"),
        }
    return list(candidates.values())


_STATUS_CACHE: tuple[float, TelegramStatus] | None = None
_STATUS_CACHE_SECONDS = 60.0


def _status_cache_get() -> TelegramStatus | None:
    if _STATUS_CACHE is None:
        return None
    created_at, status = _STATUS_CACHE
    if time.monotonic() - created_at > _STATUS_CACHE_SECONDS:
        return None
    return status


def _status_cache_set(status: TelegramStatus) -> None:
    global _STATUS_CACHE
    _STATUS_CACHE = (time.monotonic(), status)
