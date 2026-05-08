from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.config import get_settings



@dataclass(frozen=True)
class QueueConfig:
    url: str | None = None


class NullQueue:
    async def enqueue(self, name: str, payload: dict) -> dict[str, object]:
        return {"status": "skipped", "queue": "null", "task": name, "payload": payload}


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


def build_queue():
    settings = get_settings()
    if not settings.redis_url:
        return NullQueue()
    return RedisQueue(settings.redis_url)
