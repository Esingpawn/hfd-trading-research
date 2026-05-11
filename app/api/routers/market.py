from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import SessionDep
from app.api.shared import (
    _confirmation_from_items,
    _confirmation_snapshots,
    _latest_collection_cache_token,
    _latest_prices,
    _latest_snapshots_for_symbols,
    _market_cache_get,
    _market_cache_set,
)
from app.constants import ASSETS, CORE_INDICATORS, TIMEFRAMES
from app.models import SignalSnapshot
from app.services.signal_weights import build_signal_weight_map
from app.services.strategy import _score_states, _snapshot_is_fresh, _state_from_snapshot

router = APIRouter()


@router.get("/snapshots")
async def recent_snapshots(
    session: SessionDep,
    limit: int = Query(default=20, ge=1, le=200),
) -> list[dict[str, object]]:
    rows = await session.execute(
        select(SignalSnapshot).order_by(SignalSnapshot.created_at.desc()).limit(limit)
    )
    snapshots = rows.scalars().all()
    return [
        {
            "id": item.id,
            "symbol": item.symbol,
            "asset_tier": item.asset_tier,
            "timeframe": item.timeframe,
            "interval": item.interval,
            "indicator": item.indicator,
            "collected_at": item.collected_at,
            "summary": item.summary_payload,
        }
        for item in snapshots
    ]


@router.get("/market/overview")
async def market_overview(session: SessionDep) -> list[dict[str, object]]:
    cache_token = await _latest_collection_cache_token(session)
    cached = _market_cache_get(cache_token)
    if cached is not None:
        return cached
    symbols = [f"{coin}USDT" for coin in ASSETS]
    prices = await _latest_prices(session, symbols)
    snapshots = await _latest_snapshots_for_symbols(session, symbols)
    confirmations = await _confirmation_snapshots(session, symbols)
    signal_weights = await build_signal_weight_map(session)
    rows: list[dict[str, object]] = []
    for coin, asset in ASSETS.items():
        symbol = f"{coin}USDT"
        price = prices.get(symbol)
        states = [
            _state_from_snapshot(
                snapshots.get((symbol, timeframe_name, "smart_money_cost")),
                timeframe_name,
                timeframe.interval,
                price,
            )
            for timeframe_name, timeframe in TIMEFRAMES.items()
        ]
        snapshots_by_indicator: dict[str, SignalSnapshot] = {}
        stale_indicators: list[str] = []
        for indicator in CORE_INDICATORS:
            snapshot = snapshots.get((symbol, "*", indicator))
            if snapshot is None:
                continue
            if _snapshot_is_fresh(snapshot):
                snapshots_by_indicator[indicator] = snapshot
            else:
                stale_indicators.append(indicator)
        evaluation = _score_states(
            symbol,
            asset.tier,
            price,
            states,
            snapshots_by_indicator,
            stale_indicators=stale_indicators,
            signal_weights=signal_weights,
        )
        confirmation = _confirmation_from_items(
            confirmations.get(symbol, []),
            evaluation.direction,
            current_risk=evaluation.risk_payload,
        )
        evaluation.risk_payload["confirmation"] = confirmation
        states_by_timeframe = {state.timeframe: state for state in evaluation.states}
        rows.append(
            {
                "symbol": f"{coin}USDT",
                "coin": coin,
                "tier": asset.tier,
                "direction": evaluation.direction,
                "score": evaluation.score,
                "weighted_score": (evaluation.risk_payload.get("score_breakdown") or {}).get("weighted_score"),
                "structure_score": (evaluation.risk_payload.get("score_breakdown") or {}).get("structure_score"),
                "execution_score": (evaluation.risk_payload.get("score_breakdown") or {}).get("execution_score"),
                "decision": evaluation.decision,
                "price": evaluation.price,
                "risk": evaluation.risk_payload,
                "reason": evaluation.reason,
                "modules": evaluation.modules,
                "warnings": evaluation.reason.get("warnings", []),
                "required_missing_indicators": evaluation.reason.get(
                    "required_missing_indicators",
                    evaluation.reason.get("missing_indicators", []),
                ),
                "stale_indicators": evaluation.reason.get("stale_indicators", []),
                "missing_indicators": evaluation.reason.get("missing_indicators", []),
                "short": states_by_timeframe.get("short").bias if states_by_timeframe.get("short") else "missing",
                "mid": states_by_timeframe.get("mid").bias if states_by_timeframe.get("mid") else "missing",
                "long": states_by_timeframe.get("long").bias if states_by_timeframe.get("long") else "missing",
            }
        )
    _market_cache_set(rows, cache_token)
    return rows
