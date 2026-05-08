from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CacheConfig:
    url: str | None = None


class NullCache:
    async def get(self, _key: str):
        return None

    async def set(self, _key: str, _value, *, ttl_seconds: int | None = None) -> None:
        return None
