from __future__ import annotations

from dataclasses import dataclass



@dataclass(frozen=True)
class QueueConfig:
    url: str | None = None


class NullQueue:
    async def enqueue(self, name: str, payload: dict) -> dict[str, object]:
        return {"status": "skipped", "queue": "null", "task": name, "payload": payload}
