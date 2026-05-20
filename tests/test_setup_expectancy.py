from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.domain.setup_expectancy import setup_expectancy_rows


def trade(
    signal_key: str,
    *,
    strategy_id: str = "liquidity_sweep_reversal",
    strategy_name: str = "扫损反转",
    market_state: str = "liquidity_hunt_reversal",
    symbol: str = "HYPEUSDT",
    direction: str = "long",
    pnl: float | None,
    r_multiple: float | None = None,
    exit_reason: str = "take_profit",
    opened_at: datetime | None = None,
) -> SimpleNamespace:
    opened = opened_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=signal_key,
        strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
        symbol=symbol,
        timeframe="short",
        direction=direction,
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=103.0,
        status="closed",
        exit_price=102.0,
        exit_reason=exit_reason,
        pnl=pnl,
        r_multiple=r_multiple,
        mfe=0.025,
        mae=-0.006,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=90),
        context={
            "candidate_snapshot": {
                "strategy_family": "darkflow_trade_candidates_v1",
                "strategy_id": strategy_id,
                "strategy_name": strategy_name,
                "setup_type": "first_touch_reversal",
                "market_state": market_state,
                "timeframe": "short",
            },
            "shadow_plan_fingerprint": signal_key,
        },
    )


def test_setup_expectancy_groups_valid_shadow_forward_outcomes_by_setup_identity() -> None:
    rows = setup_expectancy_rows(
        [
            trade("win-1", pnl=0.03, r_multiple=2.0),
            trade("loss-1", pnl=-0.01, r_multiple=-1.0, exit_reason="shadow_forward_time_exit"),
            trade("invalid-1", pnl=None, r_multiple=None, exit_reason="take_profit"),
            trade(
                "other-1",
                strategy_id="trend_ride_extension",
                strategy_name="趋势延展",
                market_state="trend_extension",
                symbol="BTCUSDT",
                pnl=0.02,
                r_multiple=1.5,
            ),
        ],
        evidence_source="shadow_forward",
    )

    by_group = {row["group_key"]: row for row in rows}
    row = by_group["darkflow_trade_candidates_v1|first_touch_reversal|liquidity_sweep_reversal|HYPEUSDT|long|short|liquidity_hunt_reversal|shadow_forward"]

    assert row["evidence_source"] == "shadow_forward"
    assert row["sample_count"] == 2
    assert row["invalid_outcome_trades"] == 1
    assert row["win_rate"] == pytest.approx(0.5)
    assert row["profit_factor"] == pytest.approx(3.0)
    assert row["avg_r_multiple"] == pytest.approx(0.5)
    assert row["median_r_multiple"] == pytest.approx(0.5)
    assert row["time_exit_share"] == pytest.approx(0.5)
    assert row["avg_mfe"] == pytest.approx(0.025)
    assert row["avg_mae"] == pytest.approx(-0.006)
