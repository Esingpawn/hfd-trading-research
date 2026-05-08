from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.config import get_settings



@dataclass(frozen=True)
class QueueConfig:
    url: str | None = None


@dataclass(frozen=True)
class QueueMessage:
    task: str
    payload: dict[str, Any]
    raw: str


class NullQueue:
    async def enqueue(self, name: str, payload: dict) -> dict[str, object]:
        return {"status": "skipped", "queue": "null", "task": name, "payload": payload}

    async def dequeue(self, *, timeout_seconds: int = 5) -> QueueMessage | None:
        return None

    async def length(self) -> int:
        return 0


class RedisQueue:
    def __init__(self, url: str, queue_name: str = "hfd:tasks") -> None:
        self.url = url
        self.queue_name = queue_name

    async def enqueue(self, name: str, payload: dict[str, Any]) -> dict[str, object]:
        from redis.asyncio import Redis

        client = Redis.from_url(self.url, decode_responses=True)
        try:
            message = json.dumps({"task": name, "payload": payload}, ensure_ascii=False)
            length = await client.rpush(self.queue_name, message)
            return {"status": "queued", "queue": self.queue_name, "task": name, "length": length}
        finally:
            await client.aclose()

    async def dequeue(self, *, timeout_seconds: int = 5) -> QueueMessage | None:
        from redis.asyncio import Redis

        client = Redis.from_url(self.url, decode_responses=True)
        try:
            result = await client.blpop(self.queue_name, timeout=timeout_seconds)
            if result is None:
                return None
            _queue, raw = result
            return decode_task_message(str(raw))
        finally:
            await client.aclose()

    async def length(self) -> int:
        from redis.asyncio import Redis

        client = Redis.from_url(self.url, decode_responses=True)
        try:
            return int(await client.llen(self.queue_name))
        finally:
            await client.aclose()


def decode_task_message(raw: str) -> QueueMessage:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("queue message must be a JSON object")
    task = payload.get("task")
    body = payload.get("payload")
    if not isinstance(task, str) or not task:
        raise ValueError("queue message is missing task")
    if not isinstance(body, dict):
        raise ValueError("queue message payload must be an object")
    return QueueMessage(task=task, payload=body, raw=raw)


def build_queue():
    settings = get_settings()
    if not settings.redis_url:
        return NullQueue()
    return RedisQueue(settings.redis_url)
