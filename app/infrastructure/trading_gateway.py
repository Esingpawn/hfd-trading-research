from __future__ import annotations

from typing import Any


class TradingGatewayError(RuntimeError):
    pass


class PaperTradingGateway:
    async def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "accepted",
            "gateway": "paper",
            "client_order_id": payload.get("client_order_id"),
        }


class DisabledLiveTradingGateway:
    async def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise TradingGatewayError("live trading gateway is not configured")


def build_trading_gateway(mode: str, gateway_name: str = "disabled"):
    if mode == "paper":
        return PaperTradingGateway()
    return DisabledLiveTradingGateway()
