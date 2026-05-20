from __future__ import annotations

from statistics import mean, median
from typing import Any

from app.domain.shadow_forward_samples import candidate_snapshot
from app.domain.trade_outcomes import summarize_trade_outcomes


DEFAULT_STRATEGY_FAMILY = "darkflow_trade_candidates_v1"
DEFAULT_SETUP_TYPE = "unknown_setup"
TIME_EXIT_REASONS = {"shadow_forward_time_exit", "time_exit"}


def setup_expectancy_rows(trades: list[Any], *, evidence_source: str) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str, str, str, str, str, str], list[Any]] = {}
    for trade in trades:
        identity = setup_identity(trade, evidence_source=evidence_source)
        buckets.setdefault(_identity_key(identity), []).append(trade)
    rows = [setup_expectancy_row(identity_key=key, trades=items) for key, items in buckets.items()]
    return sorted(
        rows,
        key=lambda row: (
            int(row.get("sample_count") or 0),
            _sort_number(row.get("profit_factor")),
            _sort_number(row.get("win_rate")),
        ),
        reverse=True,
    )


def setup_expectancy_row(
    *,
    identity_key: tuple[str, str, str, str, str, str, str, str],
    trades: list[Any],
) -> dict[str, Any]:
    (
        strategy_family,
        setup_type,
        strategy_id,
        symbol,
        direction,
        timeframe,
        market_state,
        evidence_source,
    ) = identity_key
    stats = summarize_trade_outcomes(trades, no_loss_profit_factor=999.0, drawdown_mode="compound")
    valid_closed = [trade for trade in trades if _is_valid_closed(trade)]
    exit_counts = exit_reason_counts(trades)
    valid_exit_counts = exit_reason_counts(valid_closed)
    r_values = _numbers(getattr(trade, "r_multiple", None) for trade in valid_closed)
    mfe_values = _numbers(getattr(trade, "mfe", None) for trade in valid_closed)
    mae_values = _numbers(getattr(trade, "mae", None) for trade in valid_closed)
    strategy_names = [
        str(candidate_snapshot(trade).get("strategy_name") or "").strip()
        for trade in trades
        if str(candidate_snapshot(trade).get("strategy_name") or "").strip()
    ]
    return {
        "group_key": "|".join(identity_key),
        "strategy_family": strategy_family,
        "setup_type": setup_type,
        "strategy_id": strategy_id,
        "strategy_name": strategy_names[0] if strategy_names else strategy_id,
        "symbol": symbol,
        "direction": direction,
        "timeframe": timeframe,
        "market_state": market_state,
        "evidence_source": evidence_source,
        "source_trade_count": len(trades),
        "sample_count": stats["valid_outcome_trades"],
        "valid_outcome_trades": stats["valid_outcome_trades"],
        "invalid_outcome_trades": stats["invalid_outcome_trades"],
        "open_trades": stats["open_trades"],
        "closed_trades": stats["closed_trades"],
        "win_rate": stats["win_rate"],
        "avg_pnl": stats["avg_pnl"],
        "total_pnl": stats["total_pnl"],
        "profit_factor": stats["profit_factor"],
        "max_drawdown": stats["max_drawdown"],
        "avg_r_multiple": mean(r_values) if r_values else None,
        "median_r_multiple": median(r_values) if r_values else None,
        "avg_mfe": mean(mfe_values) if mfe_values else None,
        "avg_mae": mean(mae_values) if mae_values else None,
        "exit_reason_counts": exit_counts,
        "time_exit_share": exit_reason_share(valid_exit_counts, TIME_EXIT_REASONS),
        "take_profit_share": exit_reason_share(valid_exit_counts, {"take_profit", "target_hit"}),
        "stop_loss_share": exit_reason_share(valid_exit_counts, {"stop_loss"}),
    }


def setup_identity(trade: Any, *, evidence_source: str) -> dict[str, str]:
    snapshot = candidate_snapshot(trade)
    return {
        "strategy_family": str(snapshot.get("strategy_family") or DEFAULT_STRATEGY_FAMILY),
        "setup_type": str(snapshot.get("setup_type") or snapshot.get("interaction_type") or DEFAULT_SETUP_TYPE),
        "strategy_id": str(snapshot.get("strategy_id") or getattr(trade, "strategy_name", "") or "unknown"),
        "symbol": str(getattr(trade, "symbol", "") or "unknown"),
        "direction": str(getattr(trade, "direction", "") or "unknown"),
        "timeframe": str(snapshot.get("timeframe") or getattr(trade, "timeframe", "") or "unknown"),
        "market_state": str(snapshot.get("market_state") or "unknown"),
        "evidence_source": str(evidence_source or "unknown"),
    }


def exit_reason_counts(trades: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trade in trades:
        reason = str(getattr(trade, "exit_reason", None) or "open")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def exit_reason_share(counts: dict[str, int], reasons: set[str] | str) -> float | None:
    reason_set = {reasons} if isinstance(reasons, str) else reasons
    closed_total = sum(count for key, count in counts.items() if key != "open")
    if closed_total <= 0:
        return None
    return sum(count for key, count in counts.items() if key in reason_set) / closed_total


def _identity_key(identity: dict[str, str]) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        identity["strategy_family"],
        identity["setup_type"],
        identity["strategy_id"],
        identity["symbol"],
        identity["direction"],
        identity["timeframe"],
        identity["market_state"],
        identity["evidence_source"],
    )


def _is_valid_closed(trade: Any) -> bool:
    return str(getattr(trade, "status", "") or "").lower() == "closed" and _number(getattr(trade, "pnl", None)) is not None


def _numbers(values: Any) -> list[float]:
    parsed: list[float] = []
    for value in values:
        number = _number(value)
        if number is not None:
            parsed.append(number)
    return parsed


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed == parsed else None
    return None


def _sort_number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else -999.0
