from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.raw_payloads import payload_for_snapshot


@dataclass(frozen=True)
class RiskTemplate:
    min_score: float
    stop_pct: float
    target_pct: float
    risk_fraction: float
    max_open_same_symbol: int = 1


RISK_TEMPLATES: dict[str, RiskTemplate] = {
    "core": RiskTemplate(75, 0.01, 0.02, 0.005),
    "mainstream": RiskTemplate(78, 0.015, 0.03, 0.0035),
    "high_volatility": RiskTemplate(82, 0.025, 0.04, 0.0025),
}


def template_for_tier(asset_tier: str) -> RiskTemplate:
    return RISK_TEMPLATES.get(asset_tier, RISK_TEMPLATES["high_volatility"])


def build_trade_levels(
    direction: str,
    entry_price: float,
    asset_tier: str,
    states: list[Any] | None = None,
    snapshots: dict[str, Any] | None = None,
) -> dict[str, Any]:
    template = template_for_tier(asset_tier)
    if direction == "long":
        fallback_stop_loss = entry_price * (1 - template.stop_pct)
        fallback_take_profit = entry_price * (1 + template.target_pct)
    elif direction == "short":
        fallback_stop_loss = entry_price * (1 + template.stop_pct)
        fallback_take_profit = entry_price * (1 - template.target_pct)
    else:
        raise ValueError(f"Unsupported direction: {direction}")
    stop_plan = build_stop_plan(
        direction=direction,
        entry_price=entry_price,
        fallback_stop_loss=fallback_stop_loss,
        asset_tier=asset_tier,
        states=states or [],
        snapshots=snapshots or {},
    )
    target_plan = build_target_plan(
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_plan["stop_loss"],
        fallback_take_profit=fallback_take_profit,
        asset_tier=asset_tier,
        states=states or [],
        snapshots=snapshots or {},
    )
    return {
        "entry_price": entry_price,
        "stop_loss": stop_plan["stop_loss"],
        "take_profit": target_plan["primary_target"],
        "risk_fraction": template.risk_fraction,
        "stop_pct": abs(entry_price - stop_plan["stop_loss"]) / entry_price,
        "target_pct": abs(target_plan["primary_target"] - entry_price) / entry_price,
        "stop_source": stop_plan["source"],
        "stop_reason": stop_plan["reason"],
        "stop_candidates": stop_plan["candidates"],
        "target_source": target_plan["source"],
        "target_reason": target_plan["reason"],
        "target_candidates": target_plan["candidates"],
        "fallback_stop_loss": fallback_stop_loss,
        "fallback_take_profit": fallback_take_profit,
    }


def build_stop_plan(
    direction: str,
    entry_price: float,
    fallback_stop_loss: float,
    asset_tier: str,
    states: list[Any],
    snapshots: dict[str, Any],
) -> dict[str, Any]:
    template = template_for_tier(asset_tier)
    min_stop_pct = template.stop_pct * 0.55
    max_stop_pct = {
        "core": 0.055,
        "mainstream": 0.07,
        "high_volatility": 0.10,
    }.get(asset_tier, 0.10)
    buffer_pct = {
        "core": 0.0015,
        "mainstream": 0.0025,
        "high_volatility": 0.004,
    }.get(asset_tier, 0.004)
    candidates = _stop_candidates(direction, entry_price, states, snapshots, buffer_pct)
    valid = [
        item
        for item in candidates
        if min_stop_pct <= item["stop_pct"] <= max_stop_pct
    ]
    if valid:
        selected = sorted(
            valid,
            key=lambda item: (
                item["stop_pct"],
                -item["priority"],
                -item["confidence"],
            ),
        )[0]
        return {
            "stop_loss": selected["price"],
            "source": selected["source"],
            "reason": selected["reason"],
            "candidates": candidates[:8],
        }
    return {
        "stop_loss": fallback_stop_loss,
        "source": "risk_template_stop",
        "reason": "暗流失效位过近或过远，使用固定风控止损兜底。",
        "candidates": candidates[:8],
    }


def build_target_plan(
    direction: str,
    entry_price: float,
    stop_loss: float,
    fallback_take_profit: float,
    asset_tier: str,
    states: list[Any],
    snapshots: dict[str, Any],
) -> dict[str, Any]:
    stop_distance = abs(entry_price - stop_loss)
    min_r = 1.15 if asset_tier == "high_volatility" else 1.25
    max_primary_r = 4.0 if asset_tier == "high_volatility" else 5.0
    candidates = _target_candidates(direction, entry_price, stop_distance, states, snapshots)
    valid = [
        item
        for item in candidates
        if min_r <= item["r_multiple"] <= max_primary_r
    ]
    if valid:
        selected = sorted(
            valid,
            key=lambda item: (
                item["r_multiple"],
                -item["priority"],
                -item["confidence"],
                abs(item["price"] - entry_price),
            ),
        )[0]
        return {
            "primary_target": selected["price"],
            "source": selected["source"],
            "reason": selected["reason"],
            "candidates": candidates[:8],
        }
    return {
        "primary_target": fallback_take_profit,
        "source": "risk_reward_template",
        "reason": "暗流目标位不足或 R 倍数过低，使用固定风控模板兜底。",
        "candidates": candidates[:8],
    }


def _target_candidates(
    direction: str,
    entry_price: float,
    stop_distance: float,
    states: list[Any],
    snapshots: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for state in states:
        price = getattr(state, "avg_price", None)
        timeframe = getattr(state, "timeframe", "unknown")
        if _is_profitable_target(direction, entry_price, price):
            rows.append(
                _candidate(
                    price=price,
                    entry_price=entry_price,
                    stop_distance=stop_distance,
                    direction=direction,
                    source=f"{timeframe}_smart_money_cost",
                    reason=f"{timeframe} 成本带可作为顺方向止盈参考。",
                    priority=2.0 if timeframe in ("mid", "long") else 1.5,
                    confidence=0.55,
                )
            )
    for indicator, snapshot in snapshots.items():
        payload = payload_for_snapshot(snapshot)
        for source_key, priority in (
            ("heatmap_data", 3.0),
            ("smart_money_cost", 2.0),
            ("order_blocks", 1.8),
            ("volume_profile", 1.4),
            ("micro_poc", 1.3),
        ):
            for item in payload.get(source_key) or []:
                price = _extract_price(item)
                if not _is_profitable_target(direction, entry_price, price):
                    continue
                intensity = _confidence_from_item(item)
                rows.append(
                    _candidate(
                        price=price,
                        entry_price=entry_price,
                        stop_distance=stop_distance,
                        direction=direction,
                        source=f"{indicator}.{source_key}",
                        reason=f"{indicator} 的 {source_key} 给出顺方向流动性/成交目标。",
                        priority=priority,
                        confidence=intensity,
                    )
                )
            if len(rows) > 80:
                break
    unique: dict[tuple[str, float], dict[str, Any]] = {}
    for row in rows:
        key = (row["source"], round(row["price"], 8))
        existing = unique.get(key)
        if existing is None or row["confidence"] > existing["confidence"]:
            unique[key] = row
    return sorted(
        unique.values(),
        key=lambda item: (-item["priority"], item["r_multiple"], -item["confidence"]),
    )


def _stop_candidates(
    direction: str,
    entry_price: float,
    states: list[Any],
    snapshots: dict[str, Any],
    buffer_pct: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for state in states:
        anchor_price = getattr(state, "avg_price", None)
        timeframe = getattr(state, "timeframe", "unknown")
        if _is_stop_anchor(direction, entry_price, anchor_price):
            rows.append(
                _stop_candidate(
                    anchor_price=anchor_price,
                    entry_price=entry_price,
                    direction=direction,
                    buffer_pct=buffer_pct,
                    source=f"{timeframe}_smart_money_cost",
                    reason=f"{timeframe} 成本带被跌破/突破后，当前方向假设失效。",
                    priority=2.2 if timeframe in ("mid", "long") else 1.6,
                    confidence=0.55,
                )
            )
    for indicator, snapshot in snapshots.items():
        payload = payload_for_snapshot(snapshot)
        for source_key, priority in (
            ("heatmap_data", 3.0),
            ("smart_money_cost", 2.3),
            ("order_blocks", 2.0),
            ("volume_profile", 1.6),
            ("micro_poc", 1.4),
        ):
            for item in payload.get(source_key) or []:
                anchor_price = _extract_price(item)
                if not _is_stop_anchor(direction, entry_price, anchor_price):
                    continue
                intensity = _confidence_from_item(item)
                rows.append(
                    _stop_candidate(
                        anchor_price=anchor_price,
                        entry_price=entry_price,
                        direction=direction,
                        buffer_pct=buffer_pct,
                        source=f"{indicator}.{source_key}",
                        reason=f"{indicator} 的 {source_key} 给出反向失效锚点，止损放在锚点外侧。",
                        priority=priority,
                        confidence=intensity,
                    )
                )
            if len(rows) > 80:
                break
    unique: dict[tuple[str, float], dict[str, Any]] = {}
    for row in rows:
        key = (row["source"], round(row["anchor_price"], 8))
        existing = unique.get(key)
        if existing is None or row["confidence"] > existing["confidence"]:
            unique[key] = row
    return sorted(
        unique.values(),
        key=lambda item: (-item["priority"], item["stop_pct"], -item["confidence"]),
    )


def _stop_candidate(
    anchor_price: float,
    entry_price: float,
    direction: str,
    buffer_pct: float,
    source: str,
    reason: str,
    priority: float,
    confidence: float,
) -> dict[str, Any]:
    price = anchor_price * (1 - buffer_pct) if direction == "long" else anchor_price * (1 + buffer_pct)
    stop_pct = abs(entry_price - price) / entry_price if entry_price else 0
    return {
        "price": price,
        "anchor_price": anchor_price,
        "source": source,
        "reason": reason,
        "priority": priority,
        "confidence": round(confidence, 4),
        "stop_pct": round(stop_pct, 4),
        "buffer_pct": buffer_pct,
    }


def _candidate(
    price: float,
    entry_price: float,
    stop_distance: float,
    direction: str,
    source: str,
    reason: str,
    priority: float,
    confidence: float,
) -> dict[str, Any]:
    reward = price - entry_price if direction == "long" else entry_price - price
    r_multiple = reward / stop_distance if stop_distance else 0
    return {
        "price": price,
        "source": source,
        "reason": reason,
        "priority": priority,
        "confidence": round(confidence, 4),
        "r_multiple": round(r_multiple, 4),
    }


def _is_profitable_target(direction: str, entry_price: float, price: Any) -> bool:
    if not isinstance(price, (int, float)) or price <= 0:
        return False
    if direction == "long":
        return price > entry_price
    return price < entry_price


def _is_stop_anchor(direction: str, entry_price: float, price: Any) -> bool:
    if not isinstance(price, (int, float)) or price <= 0:
        return False
    if direction == "long":
        return price < entry_price
    return price > entry_price


def _confidence_from_item(item: Any) -> float:
    if not isinstance(item, dict):
        return 0.35
    value = item.get("intensity", 0.35)
    if not isinstance(value, (int, float)):
        return 0.35
    return min(max(float(value), 0.2), 1.0)


def _extract_price(item: Any) -> float | None:
    if isinstance(item, dict):
        for key in ("price", "avg_price", "poc", "level", "mid", "value"):
            value = item.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        low = item.get("low")
        high = item.get("high")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            return float((low + high) / 2)
    if isinstance(item, (list, tuple)):
        for value in item[1:]:
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
    return None
