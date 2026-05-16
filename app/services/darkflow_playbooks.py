from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExperimentRun, FeatureEvent, FeatureLabel
from app.services.darkflow_rules import OFFICIAL_INDICATOR_RULES, official_rule_for_internal_indicator
from app.services.feature_candidates import research_query_max_limit
from app.services.features import FEATURE_HORIZONS


DEFAULT_DARKFLOW_PLAYBOOK_LIMIT = 5000
DEFAULT_CONFIRMATION_WINDOW_MINUTES = 90
DEFAULT_MIN_SAMPLES = 30
DEFAULT_MIN_WIN_RATE = 0.52
DEFAULT_MIN_PROFIT_FACTOR = 1.1
DEFAULT_MIN_AVG_RETURN = 0.0


@dataclass(frozen=True)
class DarkflowPlaybook:
    key: str
    display_name: str
    thesis: str
    entry_indicators: tuple[str, ...]
    confirmation_indicators: tuple[str, ...]
    blocker_indicators: tuple[str, ...]
    target_indicators: tuple[str, ...]
    policy: str


PLAYBOOKS: tuple[DarkflowPlaybook, ...] = (
    DarkflowPlaybook(
        key="pullback_to_cost",
        display_name="成本带/撑压回踩",
        thesis="顺大级别成本结构，等待趋势成本带、撑压、POC、HVN 或机构 VWAP 回踩后的反应。",
        entry_indicators=(
            "smart_money_cost",
            "trend_price",
            "micro_poc",
            "hvn_nodes",
            "inst_volume_profile",
            "inst_vwap",
        ),
        confirmation_indicators=("imbalance", "cross_exchange_resonance", "liq_heatmap", "liquidity_sweep"),
        blocker_indicators=("trend_exhaustion", "time_exhaustion", "volume_exhaustion", "max_drawdown_tolerance"),
        target_indicators=("liq_heatmap", "liquidation_fuel", "fair_value_gap", "hvn_nodes"),
        policy="research_only_entry_playbook",
    ),
    DarkflowPlaybook(
        key="liquidity_sweep_reversal",
        display_name="清算/扫损反转",
        thesis="价格扫掉清算带、散户止损或连环爆仓区后快速收回，优先验证反向交易而不是顺刺穿追单。",
        entry_indicators=("liquidity_sweep", "liq_heatmap", "retail_stop_loss", "cascade_liquidation_zones"),
        confirmation_indicators=("imbalance", "micro_poc", "trend_price", "liquidation_fuel"),
        blocker_indicators=("power_imbalance", "cross_exchange_resonance"),
        target_indicators=("liq_heatmap", "micro_poc", "trend_price", "liquidation_fuel"),
        policy="research_only_reversal_playbook",
    ),
    DarkflowPlaybook(
        key="breakout_confirmation",
        display_name="结构突破确认",
        thesis="CHoCH、跨所大单、订单簿/多空力量确认同向时，才把突破当成可研究信号。",
        entry_indicators=("inst_choch", "cross_exchange_resonance", "imbalance", "power_imbalance", "ob_decay"),
        confirmation_indicators=("cross_exchange_resonance", "imbalance", "power_imbalance", "fair_value_gap", "liq_heatmap"),
        blocker_indicators=("trend_exhaustion", "retail_stop_loss", "liquidity_sweep"),
        target_indicators=("fair_value_gap", "liq_heatmap", "liquidity_vacuum", "trend_price"),
        policy="research_only_breakout_playbook",
    ),
    DarkflowPlaybook(
        key="trend_ride_extension",
        display_name="趋势持仓延展",
        thesis="趋势未耗尽时，用机构均价、动态防线、燃料库和清算磁吸延后止盈，专门纠正过早止盈问题。",
        entry_indicators=("inst_vwap", "trailing_vwap", "smart_money_cost", "trend_price", "liquidation_fuel"),
        confirmation_indicators=("cross_exchange_resonance", "imbalance", "power_imbalance", "trend_saturation"),
        blocker_indicators=(
            "trend_exhaustion",
            "time_exhaustion",
            "trend_roi",
            "volume_exhaustion",
            "max_drawdown_tolerance",
        ),
        target_indicators=("liquidation_fuel", "liq_heatmap", "trend_roi", "fair_value_gap"),
        policy="research_only_exit_optimization_playbook",
    ),
    DarkflowPlaybook(
        key="exhaustion_exit_filter",
        display_name="耗尽/死亡线退出过滤",
        thesis="趋势收益、时间、资金和回撤极限接近教程定义的死亡线时，用作减仓、保本或禁止追单的证据。",
        entry_indicators=(
            "trend_exhaustion",
            "time_exhaustion",
            "trend_roi",
            "volume_exhaustion",
            "max_drawdown_tolerance",
            "trend_saturation",
        ),
        confirmation_indicators=("liq_heatmap", "imbalance", "trend_exhaustion", "time_exhaustion"),
        blocker_indicators=("cross_exchange_resonance", "power_imbalance"),
        target_indicators=("trend_roi", "liq_heatmap"),
        policy="research_only_exit_filter_not_opening",
    ),
    DarkflowPlaybook(
        key="vacuum_acceleration",
        display_name="真空区/黑洞加速",
        thesis="FVG、流动性黑洞和筹码低谷代表价格容易快速穿越的区域，只在有结构确认时测试单边延续。",
        entry_indicators=("fair_value_gap", "liquidity_vacuum", "inst_volume_profile", "trend_purity", "poc_shift"),
        confirmation_indicators=("inst_choch", "imbalance", "power_imbalance", "cross_exchange_resonance"),
        blocker_indicators=("hvn_nodes", "smart_money_cost", "trend_exhaustion"),
        target_indicators=("hvn_nodes", "liq_heatmap", "liquidation_fuel"),
        policy="research_only_acceleration_playbook",
    ),
)


async def darkflow_playbook_backtest(
    session: AsyncSession,
    *,
    horizon: str = "4h",
    limit: int = DEFAULT_DARKFLOW_PLAYBOOK_LIMIT,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_win_rate: float = DEFAULT_MIN_WIN_RATE,
    min_profit_factor: float = DEFAULT_MIN_PROFIT_FACTOR,
    min_avg_return: float = DEFAULT_MIN_AVG_RETURN,
    confirmation_window_minutes: int = DEFAULT_CONFIRMATION_WINDOW_MINUTES,
    persist: bool = False,
) -> dict[str, Any]:
    _validate_horizon(horizon)
    requested_limit = int(limit)
    effective_limit = _bounded_limit(requested_limit)
    pairs = await _labeled_pairs(session, horizon=horizon, limit=effective_limit)
    report = _darkflow_playbook_report(
        pairs,
        horizon=horizon,
        limit=effective_limit,
        requested_limit=requested_limit,
        min_samples=min_samples,
        min_win_rate=min_win_rate,
        min_profit_factor=min_profit_factor,
        min_avg_return=min_avg_return,
        confirmation_window_minutes=confirmation_window_minutes,
    )
    if persist:
        report["experiment_run"] = await _persist_report(
            session,
            report=report,
            horizon=horizon,
            limit=effective_limit,
            requested_limit=requested_limit,
            min_samples=min_samples,
            min_win_rate=min_win_rate,
            min_profit_factor=min_profit_factor,
            min_avg_return=min_avg_return,
            confirmation_window_minutes=confirmation_window_minutes,
        )
    return report


async def latest_darkflow_playbook_backtest(
    session: AsyncSession,
    *,
    horizon: str = "4h",
) -> dict[str, Any]:
    _validate_horizon(horizon)
    row = await session.scalar(
        select(ExperimentRun)
        .where(ExperimentRun.name == f"darkflow_playbook_backtest_{horizon}", ExperimentRun.status == "research")
        .order_by(ExperimentRun.created_at.desc())
        .limit(1)
    )
    if row is None:
        return {"materialized": False, "horizon": horizon, "playbooks": []}
    metrics = dict(row.metrics or {})
    metrics["materialized"] = True
    metrics["source_experiment_run_id"] = row.id
    metrics["generated_at"] = _iso(row.created_at)
    return metrics


def darkflow_playbook_catalog() -> dict[str, Any]:
    return {
        "strategy_family": "darkflow_tutorial_playbooks_v1",
        "playbook_count": len(PLAYBOOKS),
        "policy": _policy(),
        "playbooks": [asdict(item) for item in PLAYBOOKS],
    }


async def _labeled_pairs(
    session: AsyncSession,
    *,
    horizon: str,
    limit: int,
) -> list[tuple[FeatureEvent, FeatureLabel]]:
    indicator_keys = _playbook_indicator_keys()
    per_indicator_limit = min(max(DEFAULT_MIN_SAMPLES * 4, limit // max(1, len(indicator_keys) // 2)), 2000)
    pairs_by_id: dict[str, tuple[FeatureEvent, FeatureLabel]] = {}
    for indicator in indicator_keys:
        rows = await session.execute(
            select(FeatureEvent, FeatureLabel)
            .select_from(FeatureEvent)
            .join(FeatureLabel, FeatureLabel.feature_event_id == FeatureEvent.id)
            .where(
                FeatureLabel.horizon == horizon,
                FeatureLabel.status == "labeled",
                FeatureLabel.return_pct.isnot(None),
                FeatureEvent.direction.in_(("long", "short")),
                FeatureEvent.indicator == indicator,
            )
            .order_by(FeatureEvent.event_ts.desc(), FeatureEvent.id.desc())
            .limit(per_indicator_limit)
        )
        for event, label in rows.all():
            pairs_by_id[event.id] = (event, label)
    return sorted(pairs_by_id.values(), key=lambda item: _aware(item[0].event_ts), reverse=True)[:limit]


def _playbook_indicator_keys() -> list[str]:
    keys: set[str] = set()
    for playbook in PLAYBOOKS:
        keys.update(playbook.entry_indicators)
        keys.update(playbook.confirmation_indicators)
        keys.update(playbook.blocker_indicators)
        keys.update(playbook.target_indicators)
    for rule in OFFICIAL_INDICATOR_RULES.values():
        if keys & ({rule.official_key} | set(rule.internal_keys)):
            keys.update(rule.internal_keys)
            keys.add(rule.official_key)
    return sorted(keys)


def _darkflow_playbook_report(
    pairs: list[tuple[FeatureEvent, FeatureLabel]],
    *,
    horizon: str,
    limit: int,
    requested_limit: int,
    min_samples: int,
    min_win_rate: float,
    min_profit_factor: float,
    min_avg_return: float,
    confirmation_window_minutes: int,
) -> dict[str, Any]:
    window = timedelta(minutes=max(1, int(confirmation_window_minutes)))
    indexed = _event_index(pairs)
    playbook_rows = []
    covered_event_ids: set[str] = set()
    for playbook in PLAYBOOKS:
        selected = [item for item in pairs if _matches_any(item[0], playbook.entry_indicators)]
        covered_event_ids.update(event.id for event, _label in selected)
        enriched = [_enrich_pair(item, playbook=playbook, indexed=indexed, window=window) for item in selected]
        stats = _stats([(item["event"], item["label"]) for item in enriched])
        confirmed_items = [item for item in enriched if item["confirmed"]]
        blocked_items = [item for item in enriched if item["blocker_near"]]
        row = {
            "key": playbook.key,
            "display_name": playbook.display_name,
            "thesis": playbook.thesis,
            "policy": playbook.policy,
            "indicators": {
                "entry": list(playbook.entry_indicators),
                "confirmation": list(playbook.confirmation_indicators),
                "blocker": list(playbook.blocker_indicators),
                "target": list(playbook.target_indicators),
            },
            "sample_count": stats["trade_count"],
            "stats": stats,
            "confirmed_sample_count": len(confirmed_items),
            "confirmation_rate": len(confirmed_items) / len(enriched) if enriched else 0.0,
            "confirmed_stats": _stats([(item["event"], item["label"]) for item in confirmed_items]),
            "blocker_near_count": len(blocked_items),
            "blocker_rate": len(blocked_items) / len(enriched) if enriched else 0.0,
            "top_segments": _top_segments([(item["event"], item["label"]) for item in enriched]),
            "top_entry_indicators": _top_indicators([(item["event"], item["label"]) for item in enriched]),
            "latest_events": _latest_events(enriched),
            "readiness": _readiness(
                stats,
                min_samples=min_samples,
                min_win_rate=min_win_rate,
                min_profit_factor=min_profit_factor,
                min_avg_return=min_avg_return,
            ),
        }
        playbook_rows.append(row)
    unmapped = [item for item in pairs if item[0].id not in covered_event_ids]
    return {
        "strategy_family": "darkflow_tutorial_playbooks_v1",
        "horizon": horizon,
        "limit": limit,
        "requested_limit": requested_limit,
        "limit_capped": requested_limit != limit,
        "labeled_count": len(pairs),
        "covered_labeled_count": len(covered_event_ids),
        "uncovered_labeled_count": len(unmapped),
        "confirmation_window_minutes": max(1, int(confirmation_window_minutes)),
        "thresholds": {
            "min_samples": int(min_samples),
            "min_win_rate": float(min_win_rate),
            "min_profit_factor": float(min_profit_factor),
            "min_avg_return": float(min_avg_return),
        },
        "policy": _policy(),
        "playbook_count": len(playbook_rows),
        "candidate_playbook_count": len([row for row in playbook_rows if row["readiness"]["status"] == "candidate"]),
        "watchlist_playbook_count": len([row for row in playbook_rows if row["readiness"]["status"] == "watchlist"]),
        "playbooks": sorted(
            playbook_rows,
            key=lambda row: (
                row["readiness"]["status"] == "candidate",
                row["readiness"]["status"] == "watchlist",
                row["confirmed_sample_count"],
                row["stats"].get("profit_factor") or 0.0,
                row["stats"].get("avg_return") or -999.0,
                row["sample_count"],
            ),
            reverse=True,
        ),
        "uncovered_top_indicators": _top_indicators(unmapped),
        "official_rule_coverage": _official_rule_coverage(pairs),
        "implementation_gap": {
            "v1_scope": "Uses standardized FeatureEvent + FeatureLabel rows as a tutorial-semantics proxy backtest.",
            "not_yet_modeled": [
                "zone first touch",
                "wick pierce and reclaim",
                "body break invalidation",
                "zone decay after repeated tests",
                "death-line/time exhaustion geometry",
                "target selection from heatmap/fuel distance",
            ],
            "next_step": "Build DarkFlowZone and DarkFlowInteraction rows from raw payload plus candles before allowing paper-opening integration.",
        },
    }


def _event_index(pairs: list[tuple[FeatureEvent, FeatureLabel]]) -> dict[tuple[str, str, str], list[FeatureEvent]]:
    indexed: dict[tuple[str, str, str], list[FeatureEvent]] = {}
    for event, _label in pairs:
        indexed.setdefault((event.symbol, event.timeframe, event.direction), []).append(event)
    for rows in indexed.values():
        rows.sort(key=lambda item: _aware(item.event_ts))
    return indexed


def _enrich_pair(
    pair: tuple[FeatureEvent, FeatureLabel],
    *,
    playbook: DarkflowPlaybook,
    indexed: dict[tuple[str, str, str], list[FeatureEvent]],
    window: timedelta,
) -> dict[str, Any]:
    event, label = pair
    peers = indexed.get((event.symbol, event.timeframe, event.direction), [])
    confirmation_hits = _nearby_indicator_hits(event, peers, set(playbook.confirmation_indicators), window=window)
    blocker_hits = _nearby_indicator_hits(event, peers, set(playbook.blocker_indicators), window=window)
    return {
        "event": event,
        "label": label,
        "confirmed": bool(confirmation_hits),
        "confirmation_hits": confirmation_hits[:5],
        "blocker_near": bool(blocker_hits),
        "blocker_hits": blocker_hits[:5],
    }


def _nearby_indicator_hits(
    event: FeatureEvent,
    peers: list[FeatureEvent],
    indicator_keys: set[str],
    *,
    window: timedelta,
) -> list[dict[str, Any]]:
    if not indicator_keys:
        return []
    hits = []
    event_ts = _aware(event.event_ts)
    for peer in peers:
        if peer.id == event.id:
            continue
        delta = abs(_aware(peer.event_ts) - event_ts)
        if delta > window:
            continue
        matched = sorted(_indicator_tokens(peer) & indicator_keys)
        if not matched:
            continue
        hits.append(
            {
                "indicator": matched[0],
                "event_id": peer.id,
                "feature_name": peer.feature_name,
                "subtype": peer.subtype,
                "event_ts": _iso(peer.event_ts),
                "delta_minutes": round(delta.total_seconds() / 60, 3),
            }
        )
    return sorted(hits, key=lambda item: item["delta_minutes"])


def _matches_any(event: FeatureEvent, indicators: tuple[str, ...]) -> bool:
    return bool(_indicator_tokens(event) & set(indicators))


def _indicator_tokens(event: FeatureEvent) -> set[str]:
    raw = {
        event.indicator,
        event.feature_name,
        event.source_payload_key,
        (event.feature_name or "").split(".", 1)[0],
        (event.source_payload_key or "").split(".", 1)[0],
    }
    tokens = {item for item in raw if item}
    expanded = set(tokens)
    for token in tokens:
        rule = official_rule_for_internal_indicator(token)
        if rule is not None:
            expanded.update(rule.internal_keys)
            expanded.add(rule.official_key)
    return expanded


def _stats(pairs: list[tuple[FeatureEvent, FeatureLabel]]) -> dict[str, Any]:
    ordered = sorted(pairs, key=lambda item: _aware(item[0].event_ts))
    values = [_float(label.return_pct) for _event, label in ordered]
    returns = [value for value in values if value is not None]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trade_count": len(returns),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": len(wins) / len(returns) if returns else None,
        "avg_return": mean(returns) if returns else None,
        "median_return": median(returns) if returns else None,
        "profit_factor": gross_win / gross_loss if gross_loss else (999.0 if gross_win else None),
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "max_drawdown": _max_drawdown(returns),
        "avg_mfe": _avg_label(ordered, "mfe"),
        "avg_mae": _avg_label(ordered, "mae"),
        "avg_strength": mean([event.strength for event, _label in ordered]) if ordered else None,
        "first_event_ts": _iso(ordered[0][0].event_ts) if ordered else None,
        "latest_event_ts": _iso(ordered[-1][0].event_ts) if ordered else None,
    }


def _top_segments(pairs: list[tuple[FeatureEvent, FeatureLabel]], *, limit: int = 8) -> list[dict[str, Any]]:
    buckets: dict[str, list[tuple[FeatureEvent, FeatureLabel]]] = {}
    for event, label in pairs:
        buckets.setdefault(f"{event.symbol}:{event.timeframe}:{event.direction}", []).append((event, label))
    rows = []
    for key, items in buckets.items():
        symbol, timeframe, direction = key.split(":", 2)
        stats = _stats(items)
        rows.append({"key": key, "symbol": symbol, "timeframe": timeframe, "direction": direction, **stats})
    return sorted(
        rows,
        key=lambda row: (row["trade_count"], row.get("profit_factor") or 0.0, row.get("avg_return") or -999.0),
        reverse=True,
    )[:limit]


def _top_indicators(pairs: list[tuple[FeatureEvent, FeatureLabel]], *, limit: int = 12) -> list[dict[str, Any]]:
    buckets: dict[str, list[tuple[FeatureEvent, FeatureLabel]]] = {}
    for event, label in pairs:
        buckets.setdefault(_canonical_indicator(event), []).append((event, label))
    rows = []
    for key, items in buckets.items():
        rows.append({"indicator": key, **_stats(items)})
    return sorted(
        rows,
        key=lambda row: (row["trade_count"], row.get("profit_factor") or 0.0, row.get("avg_return") or -999.0),
        reverse=True,
    )[:limit]


def _latest_events(rows: list[dict[str, Any]], *, limit: int = 6) -> list[dict[str, Any]]:
    latest = sorted(rows, key=lambda item: _aware(item["event"].event_ts), reverse=True)[:limit]
    result = []
    for item in latest:
        event = item["event"]
        label = item["label"]
        result.append(
            {
                "event_id": event.id,
                "symbol": event.symbol,
                "timeframe": event.timeframe,
                "direction": event.direction,
                "indicator": _canonical_indicator(event),
                "feature_name": event.feature_name,
                "subtype": event.subtype,
                "event_ts": _iso(event.event_ts),
                "return_pct": _float(label.return_pct),
                "mfe": _float(label.mfe),
                "mae": _float(label.mae),
                "confirmed": item["confirmed"],
                "confirmation_hits": item["confirmation_hits"],
                "blocker_near": item["blocker_near"],
            }
        )
    return result


def _readiness(
    stats: dict[str, Any],
    *,
    min_samples: int,
    min_win_rate: float,
    min_profit_factor: float,
    min_avg_return: float,
) -> dict[str, Any]:
    blockers = []
    if stats["trade_count"] < min_samples:
        blockers.append("sample_count_below_minimum")
    if stats["avg_return"] is None or stats["avg_return"] <= min_avg_return:
        blockers.append("avg_return_below_minimum")
    if stats["win_rate"] is None or stats["win_rate"] < min_win_rate:
        blockers.append("win_rate_below_minimum")
    if stats["profit_factor"] is None or stats["profit_factor"] < min_profit_factor:
        blockers.append("profit_factor_below_minimum")
    if not blockers:
        status = "candidate"
    elif blockers == ["sample_count_below_minimum"] and (stats["avg_return"] or 0.0) > 0:
        status = "watchlist"
    else:
        status = "rejected"
    return {"status": status, "blockers": blockers, "used_for_opening_decisions": False}


def _official_rule_coverage(pairs: list[tuple[FeatureEvent, FeatureLabel]]) -> dict[str, Any]:
    sampled_internal = {_canonical_indicator(event) for event, _label in pairs}
    covered_rules = []
    missing_rules = []
    for key, rule in OFFICIAL_INDICATOR_RULES.items():
        row = {
            "official_key": key,
            "official_name": rule.official_name,
            "internal_keys": list(rule.internal_keys),
            "implementation_status": rule.implementation_status,
        }
        if sampled_internal & set(rule.internal_keys):
            covered_rules.append(row)
        else:
            missing_rules.append(row)
    return {
        "official_rule_count": len(OFFICIAL_INDICATOR_RULES),
        "covered_rule_count": len(covered_rules),
        "missing_rule_count": len(missing_rules),
        "covered_rules": covered_rules,
        "missing_rules": missing_rules,
    }


def _canonical_indicator(event: FeatureEvent) -> str:
    for token in (event.indicator, (event.feature_name or "").split(".", 1)[0], event.source_payload_key):
        if not token:
            continue
        rule = official_rule_for_internal_indicator(token)
        if rule is not None:
            return rule.internal_keys[0]
    return event.indicator or event.feature_name or "unknown"


def _avg_label(pairs: list[tuple[FeatureEvent, FeatureLabel]], attr: str) -> float | None:
    values = [_float(getattr(label, attr)) for _event, label in pairs]
    filtered = [value for value in values if value is not None]
    return mean(filtered) if filtered else None


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    return max_dd


async def _persist_report(
    session: AsyncSession,
    *,
    report: dict[str, Any],
    horizon: str,
    limit: int,
    requested_limit: int,
    min_samples: int,
    min_win_rate: float,
    min_profit_factor: float,
    min_avg_return: float,
    confirmation_window_minutes: int,
) -> dict[str, str]:
    item = ExperimentRun(
        name=f"darkflow_playbook_backtest_{horizon}",
        status="research",
        scope={
            "horizon": horizon,
            "requested_limit": requested_limit,
            "limit": limit,
            "labeled_count": report["labeled_count"],
        },
        params={
            "min_samples": min_samples,
            "min_win_rate": min_win_rate,
            "min_profit_factor": min_profit_factor,
            "min_avg_return": min_avg_return,
            "confirmation_window_minutes": confirmation_window_minutes,
        },
        metrics={key: value for key, value in report.items() if key != "experiment_run"},
        notes="Darkflow tutorial-semantics proxy backtest. Research-only; old baseline_v0 remains control only.",
    )
    session.add(item)
    await session.commit()
    return {"id": item.id, "name": item.name, "status": item.status}


def _policy() -> dict[str, Any]:
    return {
        "research_only": True,
        "opens_live_orders": False,
        "opens_paper_trades": False,
        "changes_strategy_weights": False,
        "used_for_opening_decisions": False,
        "baseline_v0_status": "control_only",
        "promotion_requirement": "Must pass zone-interaction backtest and isolated shadow-paper before paper-scan integration.",
    }


def _bounded_limit(limit: int) -> int:
    return min(max(1, int(limit)), research_query_max_limit())


def _validate_horizon(horizon: str) -> None:
    if horizon not in FEATURE_HORIZONS:
        raise ValueError(f"Unsupported darkflow playbook horizon: {horizon}")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value is not None else None


def _float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None
