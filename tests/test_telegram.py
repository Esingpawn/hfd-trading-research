import pytest

from app.config import Settings
from app.services.telegram import TelegramClient, extract_chat_candidates


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeAsyncClient:
    requests: list[dict[str, object]] = []

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        self.requests.append(
            {"method": "GET", "url": url, "params": params, "headers": headers}
        )
        return _FakeResponse({"ok": True, "result": {"username": "hfd_bot"}})

    async def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        self.requests.append(
            {"method": "POST", "url": url, "json": json, "headers": headers}
        )
        return _FakeResponse({"ok": True, "result": {"message_id": 12, "date": 34}})


@pytest.fixture(autouse=True)
def reset_fake_client() -> None:
    _FakeAsyncClient.requests = []


def test_extract_chat_candidates_from_updates() -> None:
    updates = [
        {
            "message": {
                "chat": {"id": 123, "type": "private", "username": "alice"},
                "text": "/start",
            }
        }
    ]

    candidates = extract_chat_candidates(updates)

    assert candidates == [
        {
            "chat_id": "123",
            "type": "private",
            "title": None,
            "username": "alice",
            "first_name": None,
            "last_name": None,
            "last_text": "/start",
        }
    ]


@pytest.mark.asyncio
async def test_telegram_relay_get_me_uses_secret_header(monkeypatch) -> None:
    monkeypatch.setattr("app.services.telegram.httpx.AsyncClient", _FakeAsyncClient)
    client = TelegramClient(
        Settings(
            telegram_relay_url="https://relay.example.internal",
            telegram_relay_secret="relay-secret",
        )
    )

    result = await client.get_me()

    assert result == {"username": "hfd_bot"}
    assert _FakeAsyncClient.requests == [
        {
            "method": "GET",
            "url": "https://relay.example.internal/telegram/getMe",
            "params": None,
            "headers": {"X-HFD-Relay-Secret": "relay-secret"},
        }
    ]


@pytest.mark.asyncio
async def test_telegram_relay_send_message_without_bot_token(monkeypatch) -> None:
    monkeypatch.setattr("app.services.telegram.httpx.AsyncClient", _FakeAsyncClient)
    client = TelegramClient(
        Settings(
            telegram_chat_id="123",
            telegram_relay_url="https://relay.example.internal",
            telegram_relay_secret="relay-secret",
        )
    )

    result = await client.send_message("hello")

    assert result == {"message_id": 12, "date": 34}
    assert _FakeAsyncClient.requests == [
        {
            "method": "POST",
            "url": "https://relay.example.internal/telegram/sendMessage",
            "json": {
                "chat_id": "123",
                "text": "hello",
                "disable_web_page_preview": True,
            },
            "headers": {"X-HFD-Relay-Secret": "relay-secret"},
        }
    ]


@pytest.mark.asyncio
async def test_telegram_direct_mode_still_uses_bot_token(monkeypatch) -> None:
    monkeypatch.setattr("app.services.telegram.httpx.AsyncClient", _FakeAsyncClient)
    client = TelegramClient(Settings(telegram_bot_token="bot-token"))

    await client.get_me()

    assert _FakeAsyncClient.requests[0]["url"] == "https://api.telegram.org/botbot-token/getMe"
    assert _FakeAsyncClient.requests[0]["headers"] is None
