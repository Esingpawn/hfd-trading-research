from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Direction = Literal["long", "short"]


@dataclass(frozen=True)
class BacktestTrade:
    direction: Direction
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    exit_reason: str
    pnl_pct: float
    r_multiple: float


@dataclass(frozen=True)
class BacktestSummary:
    strategy: str
    symbol: str
    interval: str
    trade_count: int
    win_rate: float
    avg_pnl_pct: float
    total_pnl_pct: float
    profit_factor: float | None
    max_drawdown_pct: float
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BacktestResult:
    summary: BacktestSummary
    trades: list[BacktestTrade]


def run_cost_band_retest_backtest(
    payload: dict[str, Any],
    symbol: str,
    interval: str,
    stop_pct: float = 0.01,
    target_pct: float = 0.02,
    max_hold_bars: int = 24,
    limit_zones: int | None = 100,
) -> BacktestResult:
    """Static historical prototype.

    This intentionally uses HFD's current historical response. It is useful for
    screening ideas, but it can include hindsight/repaint effects from the source.
    """

    klines = payload.get("klines") or []
    zones = payload.get("smart_money_cost") or []
    ts_to_index = {int(k[0]): idx for idx, k in enumerate(klines)}
    trades: list[BacktestTrade] = []

    selected_zones = zones[-limit_zones:] if limit_zones else zones
    last_exit_index = -1

    for zone in selected_zones:
        direction = _zone_direction(zone)
        if direction is None:
            continue

        avg_price = float(zone["avg_price"])
        end_time = int(zone.get("end_time") or zone.get("last_update_time") or 0)
        start_index = ts_to_index.get(end_time)
        if start_index is None:
            start_index = _first_index_after(klines, end_time)
        if start_index is None:
            continue

        # Enter only after the source zone is complete. This still does not remove
        # all repaint risk, but avoids using same-bar hindsight in this prototype.
        entry_index = max(start_index + 1, last_exit_index + 1)
        if entry_index >= len(klines):
            continue

        touched_index = _find_retest_index(
            klines,
            entry_index,
            avg_price,
            max_search_bars=max_hold_bars,
        )
        if touched_index is None:
            continue

        exit_trade = _simulate_trade(
            klines=klines,
            entry_index=touched_index,
            direction=direction,
            entry_price=avg_price,
            stop_pct=stop_pct,
            target_pct=target_pct,
            max_hold_bars=max_hold_bars,
        )
        if exit_trade is None:
            continue
        trades.append(exit_trade)
        last_exit_index = ts_to_index.get(exit_trade.exit_ts, touched_index)

    return BacktestResult(
        summary=_summarize_trades(
            strategy="cost_band_retest_static_v0",
            symbol=symbol,
            interval=interval,
            trades=trades,
            notes=[
                "静态历史回测只用于策略初筛",
                "暗流 Pro 历史指标可能存在后验修正或重绘",
                "通过后仍必须进入实时快照纸上交易",
            ],
        ),
        trades=trades,
    )


def _zone_direction(zone: dict[str, Any]) -> Direction | None:
    zone_type = str(zone.get("type", "")).lower()
    if zone_type == "accumulation":
        return "long"
    if zone_type == "distribution":
        return "short"
    return None


def _first_index_after(klines: list[list[Any]], timestamp: int) -> int | None:
    for idx, kline in enumerate(klines):
        if int(kline[0]) >= timestamp:
            return idx
    return None


def _find_retest_index(
    klines: list[list[Any]],
    start_index: int,
    price: float,
    max_search_bars: int,
) -> int | None:
    end_index = min(len(klines), start_index + max_search_bars)
    for idx in range(start_index, end_index):
        low = float(klines[idx][3])
        high = float(klines[idx][4])
        if low <= price <= high:
            return idx
    return None


def _simulate_trade(
    klines: list[list[Any]],
    entry_index: int,
    direction: Direction,
    entry_price: float,
    stop_pct: float,
    target_pct: float,
    max_hold_bars: int,
) -> BacktestTrade | None:
    if entry_index >= len(klines):
        return None

    if direction == "long":
        stop = entry_price * (1 - stop_pct)
        target = entry_price * (1 + target_pct)
    else:
        stop = entry_price * (1 + stop_pct)
        target = entry_price * (1 - target_pct)

    end_index = min(len(klines) - 1, entry_index + max_hold_bars)
    for idx in range(entry_index + 1, end_index + 1):
        low = float(klines[idx][3])
        high = float(klines[idx][4])
        close = float(klines[idx][2])

        if direction == "long":
            if low <= stop:
                return _make_trade(direction, klines, entry_index, idx, entry_price, stop, "stop", stop_pct)
            if high >= target:
                return _make_trade(direction, klines, entry_index, idx, entry_price, target, "target", stop_pct)
        else:
            if high >= stop:
                return _make_trade(direction, klines, entry_index, idx, entry_price, stop, "stop", stop_pct)
            if low <= target:
                return _make_trade(direction, klines, entry_index, idx, entry_price, target, "target", stop_pct)

        if idx == end_index:
            return _make_trade(direction, klines, entry_index, idx, entry_price, close, "time_exit", stop_pct)
    return None


def _make_trade(
    direction: Direction,
    klines: list[list[Any]],
    entry_index: int,
    exit_index: int,
    entry_price: float,
    exit_price: float,
    exit_reason: str,
    stop_pct: float,
) -> BacktestTrade:
    if direction == "long":
        pnl_pct = (exit_price - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - exit_price) / entry_price
    return BacktestTrade(
        direction=direction,
        entry_ts=int(klines[entry_index][0]),
        entry_price=entry_price,
        exit_ts=int(klines[exit_index][0]),
        exit_price=exit_price,
        exit_reason=exit_reason,
        pnl_pct=pnl_pct,
        r_multiple=pnl_pct / stop_pct if stop_pct else 0.0,
    )


def _summarize_trades(
    strategy: str,
    symbol: str,
    interval: str,
    trades: list[BacktestTrade],
    notes: list[str],
) -> BacktestSummary:
    if not trades:
        return BacktestSummary(
            strategy=strategy,
            symbol=symbol,
            interval=interval,
            trade_count=0,
            win_rate=0.0,
            avg_pnl_pct=0.0,
            total_pnl_pct=0.0,
            profit_factor=None,
            max_drawdown_pct=0.0,
            notes=notes,
        )

    wins = [t.pnl_pct for t in trades if t.pnl_pct > 0]
    losses = [t.pnl_pct for t in trades if t.pnl_pct < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total = sum(t.pnl_pct for t in trades)
    return BacktestSummary(
        strategy=strategy,
        symbol=symbol,
        interval=interval,
        trade_count=len(trades),
        win_rate=len(wins) / len(trades),
        avg_pnl_pct=total / len(trades),
        total_pnl_pct=total,
        profit_factor=(gross_profit / gross_loss) if gross_loss else None,
        max_drawdown_pct=_max_drawdown([t.pnl_pct for t in trades]),
        notes=notes,
    )


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    return max_dd

