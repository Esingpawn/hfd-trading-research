from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings, get_settings


class HfdClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            base_url=self.settings.hfd_base_url,
            timeout=self.settings.http_timeout_seconds,
            headers={"User-Agent": self.settings.collector_user_agent},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HfdClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def fetch_price(self, coin: str) -> dict[str, Any]:
        response = await self._client.get(
            "/api/proxy/price", params={"symbol": f"{coin.upper()}USDT"}
        )
        response.raise_for_status()
        data = response.json()
        if "price" not in data:
            raise ValueError(f"HFD price response missing price for {coin}: {data}")
        return data

    async def fetch_pro_data(
        self, coin: str, interval: str, indicator: str
    ) -> dict[str, Any]:
        response = await self._client.get(
            "/api/pro/pro_data",
            params={
                "coin": coin.upper(),
                "interval": interval,
                "indicator": indicator,
            },
        )
        response.raise_for_status()
        data = response.json()
        if "klines" not in data:
            raise ValueError(
                f"HFD pro response missing klines for {coin} {interval} {indicator}"
            )
        return data

