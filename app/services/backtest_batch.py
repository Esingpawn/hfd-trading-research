from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import ASSETS, TIMEFRAMES
from app.hfd.client import HfdClient
from app.models import BacktestRun
from app.services.backtest import run_cost_band_retest_backtest


async def run_backtest_batch(
    session: AsyncSession,
    coins: list[str] | None = None,
    timeframes: list[str] | None = None,
    stop_pct: float = 0.01,
    target_pct: float = 0.02,
    max_hold_bars: int = 24,
    limit_zones: int = 100,
    persist: bool = True,
) -> dict[str, Any]:
    selected_coins = [c.upper() for c in coins] if coins else list(ASSETS)
    selected_timeframes = [t.lower() for t in timeframes] if timeframes else list(TIMEFRAMES)
    errors: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    async with HfdClient() as client:
        for coin in selected_coins:
            for timeframe in selected_timeframes:
                interval = TIMEFRAMES[timeframe].interval
                try:
                    payload = await client.fetch_pro_data(coin, interval, "smart_money_cost")
                    result = run_cost_band_retest_backtest(
                        payload=payload,
                        symbol=f"{coin}USDT",
                        interval=interval,
                        stop_pct=stop_pct,
                        target_pct=target_pct,
                        max_hold_bars=max_hold_bars,
                        limit_zones=limit_zones,
                    )
                    row = asdict(result.summary)
                    row["coin"] = coin
                    row["timeframe"] = timeframe
                    row["score"] = _ranking_score(row)
                    results.append(row)
                except Exception as exc:  # noqa: BLE001
                    errors.append({"coin": coin, "timeframe": timeframe, "error": str(exc)})

    results.sort(key=lambda row: row["score"], reverse=True)
    payload = {
        "strategy": "cost_band_retest_static_v0",
        "status": "completed" if not errors else "completed_with_errors",
        "params": {
            "stop_pct": stop_pct,
            "target_pct": target_pct,
            "max_hold_bars": max_hold_bars,
            "limit_zones": limit_zones,
        },
        "results": results,
        "errors": errors,
    }
    if persist:
        session.add(
            BacktestRun(
                strategy=payload["strategy"],
                status=payload["status"],
                requested_assets=selected_coins,
                requested_timeframes=selected_timeframes,
                params=payload["params"],
                results=results,
                errors=errors,
            )
        )
        await session.commit()
    return payload


def _ranking_score(row: dict[str, Any]) -> float:
    trade_count = row.get("trade_count") or 0
    if trade_count < 8:
        return -1.0
    profit_factor = row.get("profit_factor") or 0.0
    win_rate = row.get("win_rate") or 0.0
    max_drawdown = row.get("max_drawdown_pct") or 0.0
    return profit_factor * 2 + win_rate - max_drawdown * 5 + min(trade_count, 50) / 100

