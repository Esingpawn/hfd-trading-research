from types import SimpleNamespace

import pytest

from app.domain.trade_outcomes import build_trade_outcome, summarize_trade_outcomes


def test_build_trade_outcome_for_closed_long_target() -> None:
    trade = SimpleNamespace(
        status="closed",
        direction="long",
        entry_price=100.0,
        exit_price=104.0,
        stop_loss=98.0,
        take_profit=104.0,
        position_size=1.0,
        exit_reason="take_profit",
        pnl=0.038,
        r_multiple=1.9,
        mfe=0.05,
        mae=-0.01,
    )

    outcome = build_trade_outcome(trade, source="paper")

    assert outcome["valid"] is True
    assert outcome["missing_fields"] == []
    assert outcome["exit_reason"] == "take_profit"
    assert outcome["exit_reason_label"] == "止盈"
    assert outcome["gross_pnl"] == pytest.approx(0.04)
    assert outcome["net_pnl"] == pytest.approx(0.038)
    assert outcome["cost_impact"] == pytest.approx(0.002)
    assert outcome["r_multiple"] == pytest.approx(1.9)
    assert outcome["mfe"] == pytest.approx(0.05)
    assert outcome["mae"] == pytest.approx(-0.01)


def test_build_trade_outcome_for_closed_short_stop() -> None:
    trade = SimpleNamespace(
        status="closed",
        direction="short",
        entry_price=100.0,
        exit_price=102.0,
        stop_loss=102.0,
        take_profit=96.0,
        position_size=1.0,
        exit_reason="stop_loss",
        pnl=-0.022,
        r_multiple=-1.1,
        mfe=0.03,
        mae=-0.02,
    )

    outcome = build_trade_outcome(trade, source="shadow")

    assert outcome["valid"] is True
    assert outcome["exit_reason_label"] == "止损"
    assert outcome["gross_pnl"] == pytest.approx(-0.02)
    assert outcome["net_pnl"] == pytest.approx(-0.022)
    assert outcome["r_multiple"] == pytest.approx(-1.1)


def test_build_trade_outcome_marks_missing_closed_values_invalid_not_zero() -> None:
    trade = SimpleNamespace(
        status="closed",
        direction="long",
        entry_price=100.0,
        exit_price=None,
        stop_loss=98.0,
        take_profit=104.0,
        position_size=1.0,
        exit_reason=None,
        pnl=None,
        r_multiple=None,
        mfe=None,
        mae=None,
    )

    outcome = build_trade_outcome(trade, source="paper")

    assert outcome["valid"] is False
    assert outcome["gross_pnl"] is None
    assert outcome["net_pnl"] is None
    assert outcome["r_multiple"] is None
    assert outcome["mfe"] is None
    assert outcome["mae"] is None
    assert set(outcome["missing_fields"]) >= {"exit_price", "exit_reason", "pnl", "r_multiple", "mfe", "mae"}


@pytest.mark.parametrize(
    ("reason", "label"),
    [
        ("target_hit", "止盈"),
        ("shadow_forward_time_exit", "时间退场"),
        ("time_exit", "时间退场"),
        ("invalidated", "条件作废"),
        ("manual_close", "手动平仓"),
        ("trailing_stop", "移动止损"),
    ],
)
def test_build_trade_outcome_normalizes_exit_reason_labels(reason: str, label: str) -> None:
    trade = SimpleNamespace(
        status="closed",
        direction="long",
        entry_price=100.0,
        exit_price=101.0,
        stop_loss=98.0,
        take_profit=104.0,
        position_size=1.0,
        exit_reason=reason,
        pnl=0.01,
        r_multiple=0.5,
        mfe=0.02,
        mae=-0.005,
    )

    outcome = build_trade_outcome(trade, source="paper")

    assert outcome["exit_reason_label"] == label


def test_summarize_trade_outcomes_excludes_invalid_closed_rows_from_performance() -> None:
    trades = [
        SimpleNamespace(status="closed", pnl=0.03, r_multiple=3.0, closed_at="2026-01-01T00:00:00Z"),
        SimpleNamespace(status="closed", pnl=-0.01, r_multiple=-1.0, closed_at="2026-01-01T01:00:00Z"),
        SimpleNamespace(status="closed", pnl=None, r_multiple=None, closed_at="2026-01-01T02:00:00Z"),
        SimpleNamespace(status="open", pnl=None, r_multiple=None),
    ]

    stats = summarize_trade_outcomes(trades)

    assert stats["total_trades"] == 4
    assert stats["open_trades"] == 1
    assert stats["closed_trades"] == 3
    assert stats["valid_outcome_trades"] == 2
    assert stats["invalid_outcome_trades"] == 1
    assert stats["win_count"] == 1
    assert stats["loss_count"] == 1
    assert stats["win_rate"] == 0.5
    assert stats["avg_pnl"] == pytest.approx(0.01)
    assert stats["total_pnl"] == pytest.approx(0.02)
    assert stats["profit_factor"] == pytest.approx(3.0)
    assert stats["avg_r_multiple"] == pytest.approx(1.0)
    assert stats["max_drawdown"] > 0


def test_summarize_trade_outcomes_allows_shadow_style_profit_factor_and_drawdown() -> None:
    trades = [
        SimpleNamespace(status="closed", pnl=0.03, r_multiple=3.0, closed_at="2026-01-01T00:00:00Z"),
        SimpleNamespace(status="closed", pnl=0.02, r_multiple=2.0, closed_at="2026-01-01T01:00:00Z"),
    ]

    stats = summarize_trade_outcomes(trades, no_loss_profit_factor=999.0, drawdown_mode="additive")

    assert stats["profit_factor"] == 999.0
    assert stats["max_drawdown"] == 0.0
