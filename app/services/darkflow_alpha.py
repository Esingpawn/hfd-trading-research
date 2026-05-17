from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_lineage.registry import CORE_DARKFLOW_V2
from app.models import ShadowPaperTrade, utc_now
from app.services.darkflow_candidate_promotion import (
    DARKFLOW_V2_SHADOW_STRATEGY_NAME,
    DEFAULT_ENTRY_TOLERANCE_PCT,
    DEFAULT_MAX_CANDIDATE_AGE_HOURS,
    DEFAULT_PROMOTION_LIMIT,
    DEFAULT_SHADOW_FORWARD_LIMIT,
    refresh_darkflow_candidate_promotion,
)
from app.services.shadow_paper import mark_shadow_paper_trades


DEFAULT_ALPHA_SCOREBOARD_LIMIT = 50
DEFAULT_ALPHA_MIN_CLOSED_TRADES = 5
ALPHA_READY_MIN_CLOSED_TRADES = 30
ALPHA_READY_MIN_WIN_RATE = 0.55
ALPHA_READY_MIN_PROFIT_FACTOR = 1.25
ALPHA_READY_MAX_DRAWDOWN = 0.12
ALPHA_WATCHLIST_MIN_CLOSED_TRADES = 3
ALPHA_WATCHLIST_MAX_WIN_RATE = 0.35
ALPHA_WATCHLIST_MAX_PROFIT_FACTOR = 0.85


async def darkflow_alpha_scoreboard(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_ALPHA_SCOREBOARD_LIMIT,
    min_closed_trades: int = DEFAULT_ALPHA_MIN_CLOSED_TRADES,
) -> dict[str, Any]:
    rows = await session.scalars(
        select(ShadowPaperTrade)
        .where(ShadowPaperTrade.strategy_name == DARKFLOW_V2_SHADOW_STRATEGY_NAME)
        .order_by(ShadowPaperTrade.opened_at.desc(), ShadowPaperTrade.id.desc())
    )
    trades = list(rows.all())
    unique_trades = _unique_plan_trades(trades)
    grouped_rows = _grouped_rows(unique_trades, source_trades=trades, min_closed_trades=max(0, int(min_closed_trades)))
    scoreboard_rows = sorted(
        grouped_rows,
        key=lambda row: (
            _conclusion_rank(str(row["conclusion"])),
            int(row["closed_trades"]),
            _sort_number(row.get("profit_factor")),
            _sort_number(row.get("avg_pnl")),
        ),
        reverse=True,
    )[: max(1, int(limit))]
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "lineage": CORE_DARKFLOW_V2,
        "generated_at": _iso(utc_now()),
        "rows": scoreboard_rows,
        "totals": _totals(trades, unique_trades),
        "thresholds": {
            "min_closed_trades": max(0, int(min_closed_trades)),
            "ready_min_closed_trades": ALPHA_READY_MIN_CLOSED_TRADES,
            "ready_min_win_rate": ALPHA_READY_MIN_WIN_RATE,
            "ready_min_profit_factor": ALPHA_READY_MIN_PROFIT_FACTOR,
            "ready_max_drawdown": ALPHA_READY_MAX_DRAWDOWN,
            "watchlist_min_closed_trades": ALPHA_WATCHLIST_MIN_CLOSED_TRADES,
            "watchlist_max_win_rate": ALPHA_WATCHLIST_MAX_WIN_RATE,
            "watchlist_max_profit_factor": ALPHA_WATCHLIST_MAX_PROFIT_FACTOR,
        },
        "policy": _policy() | {"report_only": True},
    }


async def accelerate_darkflow_alpha(
    session: AsyncSession,
    *,
    candidate_limit: int = DEFAULT_PROMOTION_LIMIT,
    shadow_limit: int = DEFAULT_SHADOW_FORWARD_LIMIT,
    max_candidate_age_hours: float = DEFAULT_MAX_CANDIDATE_AGE_HOURS,
    entry_tolerance_pct: float = DEFAULT_ENTRY_TOLERANCE_PCT,
    materialize: bool = True,
    mark_first: bool = True,
    scoreboard_limit: int = DEFAULT_ALPHA_SCOREBOARD_LIMIT,
) -> dict[str, Any]:
    mark_result: dict[str, Any] = {"enabled": False, "reason": "mark_first_disabled"}
    if mark_first:
        mark_result = await mark_shadow_paper_trades(session)
    refresh = await refresh_darkflow_candidate_promotion(
        session,
        limit=candidate_limit,
        shadow_limit=shadow_limit,
        max_candidate_age_hours=max_candidate_age_hours,
        entry_tolerance_pct=entry_tolerance_pct,
        materialize=materialize,
    )
    scoreboard = await darkflow_alpha_scoreboard(session, limit=scoreboard_limit, min_closed_trades=1)
    return {
        "strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "lineage": CORE_DARKFLOW_V2,
        "generated_at": _iso(utc_now()),
        "steps": {
            "mark_shadow_trades": mark_result,
            "promotion_refresh": refresh,
            "alpha_scoreboard": scoreboard,
        },
        "policy": _policy(),
    }


def _policy() -> dict[str, Any]:
    return {
        "research_only": True,
        "lineage": CORE_DARKFLOW_V2,
        "uses_shadow_forward_only": True,
        "opens_paper_trades": False,
        "opens_live_orders": False,
        "sends_entry_notifications": False,
        "isolated_strategy_name": DARKFLOW_V2_SHADOW_STRATEGY_NAME,
        "isolated_table": "shadow_paper_trades",
        "promotion_boundary": "Alpha scoreboard can guide manual review only; it does not promote candidates to paper or live execution.",
    }


def _grouped_rows(
    unique_trades: list[ShadowPaperTrade],
    *,
    source_trades: list[ShadowPaperTrade],
    min_closed_trades: int,
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str, str, str], list[ShadowPaperTrade]] = {}
    for trade in unique_trades:
        buckets.setdefault(_group_key(trade), []).append(trade)
    source_counts = _source_counts_by_group(source_trades)
    rows: list[dict[str, Any]] = []
    for key, trades in buckets.items():
        stats = _trade_stats(trades)
        if int(stats["closed_trades"]) < min_closed_trades:
            continue
        strategy_id, symbol, direction, timeframe, market_state = key
        source_count = source_counts.get(key, len(trades))
        duplicate_count = max(0, source_count - len(trades))
        conclusion = _alpha_conclusion(stats)
        rows.append(
            {
                "strategy_id": strategy_id,
                "strategy_name": _strategy_name(trades),
                "symbol": symbol,
                "direction": direction,
                "timeframe": timeframe,
                "market_state": market_state,
                **stats,
                "unique_plan_count": len(trades),
                "source_trade_count": source_count,
                "duplicate_trade_count": duplicate_count,
                "conclusion": conclusion,
                "next_action": _next_action(conclusion, stats),
            }
        )
    return rows


def _source_counts_by_group(trades: list[ShadowPaperTrade]) -> dict[tuple[str, str, str, str, str], int]:
    counts: dict[tuple[str, str, str, str, str], int] = {}
    for trade in trades:
        key = _group_key(trade)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _group_key(trade: ShadowPaperTrade) -> tuple[str, str, str, str, str]:
    snapshot = _candidate_snapshot(trade)
    strategy_id = str(snapshot.get("strategy_id") or trade.strategy_name)
    market_state = str(snapshot.get("market_state") or "unknown")
    return (strategy_id, trade.symbol, trade.direction, trade.timeframe, market_state)


def _strategy_name(trades: list[ShadowPaperTrade]) -> str:
    for trade in trades:
        snapshot = _candidate_snapshot(trade)
        raw = snapshot.get("strategy_name")
        if raw:
            return str(raw)
    first = trades[0] if trades else None
    return first.strategy_name if first is not None else "unknown"


def _candidate_snapshot(trade: ShadowPaperTrade) -> dict[str, Any]:
    context = trade.context if isinstance(trade.context, dict) else {}
    snapshot = context.get("candidate_snapshot")
    return snapshot if isinstance(snapshot, dict) else {}


def _unique_plan_trades(trades: list[ShadowPaperTrade]) -> list[ShadowPaperTrade]:
    best_by_plan: dict[str, ShadowPaperTrade] = {}
    for trade in trades:
        key = _trade_plan_fingerprint(trade)
        current = best_by_plan.get(key)
        if current is None or _trade_plan_rank(trade) > _trade_plan_rank(current):
            best_by_plan[key] = trade
    return list(best_by_plan.values())


def _trade_plan_rank(trade: ShadowPaperTrade) -> tuple[int, datetime, str]:
    closed_rank = 1 if trade.status == "closed" and isinstance(trade.pnl, (int, float)) else 0
    observed_at = trade.closed_at or trade.opened_at or datetime.min.replace(tzinfo=timezone.utc)
    return (closed_rank, _aware(observed_at), trade.id)


def _trade_plan_fingerprint(trade: ShadowPaperTrade) -> str:
    context = trade.context if isinstance(trade.context, dict) else {}
    explicit = context.get("shadow_plan_fingerprint")
    if explicit:
        return f"explicit:{explicit}"
    snapshot = _candidate_snapshot(trade)
    entry = _number(snapshot.get("entry_price")) or float(trade.entry_price)
    stop = _number(snapshot.get("stop_price")) or float(trade.stop_loss)
    target = _number(snapshot.get("target_price")) or float(trade.take_profit)
    opened_slot = _aware(trade.opened_at).replace(minute=0, second=0, microsecond=0).isoformat() if trade.opened_at else "unknown"
    return ":".join(
        [
            "bucket",
            trade.strategy_name,
            str(snapshot.get("strategy_id") or trade.strategy_name),
            trade.symbol,
            trade.timeframe,
            trade.direction,
            opened_slot,
            _rounded_price_bucket(entry),
            _rounded_price_bucket(stop),
            _rounded_price_bucket(target),
        ]
    )


def _trade_stats(trades: list[ShadowPaperTrade]) -> dict[str, Any]:
    closed = sorted(
        [item for item in trades if item.status == "closed" and isinstance(item.pnl, (int, float))],
        key=lambda item: _aware(item.closed_at or item.opened_at or datetime.min.replace(tzinfo=timezone.utc)),
    )
    wins = [float(item.pnl) for item in closed if float(item.pnl or 0.0) > 0]
    losses = [float(item.pnl) for item in closed if float(item.pnl or 0.0) < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    returns = [float(item.pnl or 0.0) for item in closed]
    opened_times = [item.opened_at for item in trades if item.opened_at]
    closed_times = [item.closed_at for item in closed if item.closed_at]
    return {
        "open_trades": sum(1 for item in trades if item.status == "open"),
        "closed_trades": len(closed),
        "total_trades": len(trades),
        "win_rate": len(wins) / len(closed) if closed else None,
        "avg_pnl": mean(returns) if returns else None,
        "profit_factor": gross_win / gross_loss if gross_loss else (999.0 if gross_win else None),
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "max_drawdown": _max_drawdown(returns),
        "latest_opened_at": _iso(max(opened_times)) if opened_times else None,
        "latest_closed_at": _iso(max(closed_times)) if closed_times else None,
    }


def _totals(trades: list[ShadowPaperTrade], unique_trades: list[ShadowPaperTrade]) -> dict[str, Any]:
    stats = _trade_stats(unique_trades)
    return {
        **stats,
        "source_trade_count": len(trades),
        "unique_plan_count": len(unique_trades),
        "duplicate_trade_count": max(0, len(trades) - len(unique_trades)),
    }


def _alpha_conclusion(stats: dict[str, Any]) -> str:
    closed = int(stats.get("closed_trades") or 0)
    win_rate = stats.get("win_rate")
    profit_factor = stats.get("profit_factor")
    max_drawdown = stats.get("max_drawdown")
    if (
        closed >= ALPHA_READY_MIN_CLOSED_TRADES
        and isinstance(win_rate, (int, float))
        and isinstance(profit_factor, (int, float))
        and isinstance(max_drawdown, (int, float))
        and float(win_rate) >= ALPHA_READY_MIN_WIN_RATE
        and float(profit_factor) >= ALPHA_READY_MIN_PROFIT_FACTOR
        and float(max_drawdown) <= ALPHA_READY_MAX_DRAWDOWN
    ):
        return "可进入人工复核"
    if (
        closed >= ALPHA_WATCHLIST_MIN_CLOSED_TRADES
        and isinstance(win_rate, (int, float))
        and isinstance(profit_factor, (int, float))
        and float(win_rate) <= ALPHA_WATCHLIST_MAX_WIN_RATE
        and float(profit_factor) <= ALPHA_WATCHLIST_MAX_PROFIT_FACTOR
    ):
        return "暂停观察"
    if closed < ALPHA_READY_MIN_CLOSED_TRADES:
        return "样本收集中" if _positive_or_unknown(win_rate, profit_factor) else "观察名单"
    return "观察名单"


def _positive_or_unknown(win_rate: Any, profit_factor: Any) -> bool:
    if isinstance(win_rate, (int, float)) and float(win_rate) <= ALPHA_WATCHLIST_MAX_WIN_RATE:
        return False
    if isinstance(profit_factor, (int, float)) and float(profit_factor) <= ALPHA_WATCHLIST_MAX_PROFIT_FACTOR:
        return False
    return True


def _next_action(conclusion: str, stats: dict[str, Any]) -> str:
    closed = int(stats.get("closed_trades") or 0)
    if conclusion == "可进入人工复核":
        return "进入人工复核：检查最近样本、回撤和教程规则命中明细，不自动开单。"
    if conclusion == "暂停观察":
        return "暂停新增同类候选，先排查市场环境或入场规则是否失效。"
    if conclusion == "样本收集中":
        need = max(0, ALPHA_READY_MIN_CLOSED_TRADES - closed)
        return f"继续积累影子前向样本，还差约 {need} 笔闭合样本才有统计意义。"
    return "保留观察，不提高权重；等待更多闭合样本或规则修正。"


def _conclusion_rank(conclusion: str) -> int:
    return {
        "可进入人工复核": 4,
        "样本收集中": 3,
        "观察名单": 2,
        "暂停观察": 1,
    }.get(conclusion, 0)


def _max_drawdown(returns: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _rounded_price_bucket(price: float) -> str:
    if price >= 1000:
        digits = 0
    elif price >= 100:
        digits = 1
    elif price >= 10:
        digits = 2
    elif price >= 1:
        digits = 4
    else:
        digits = 6
    return f"{round(float(price), digits):.{digits}f}"


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed == parsed else None
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if parsed == parsed else None
    return None


def _sort_number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else -999.0


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value is not None else None
