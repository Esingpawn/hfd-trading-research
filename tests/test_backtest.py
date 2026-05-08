from app.services.backtest import run_cost_band_retest_backtest


def test_cost_band_retest_backtest_can_create_winning_long_trade() -> None:
    payload = {
        "klines": [
            [1000, 100, 100, 99, 101, 10],
            [2000, 100, 100, 99, 101, 10],
            [3000, 100, 101, 99.5, 101, 10],
            [4000, 101, 103, 100.5, 103, 10],
        ],
        "smart_money_cost": [
            {
                "type": "Accumulation",
                "avg_price": 100,
                "start_time": 1000,
                "end_time": 1000,
            }
        ],
    }

    result = run_cost_band_retest_backtest(
        payload,
        symbol="BTCUSDT",
        interval="30m",
        stop_pct=0.01,
        target_pct=0.02,
        max_hold_bars=3,
    )

    assert result.summary.trade_count == 1
    assert result.summary.win_rate == 1.0
    assert result.trades[0].direction == "long"
    assert result.trades[0].exit_reason == "target"


def test_cost_band_retest_backtest_handles_no_trades() -> None:
    result = run_cost_band_retest_backtest(
        {"klines": [], "smart_money_cost": []},
        symbol="BTCUSDT",
        interval="30m",
    )

    assert result.summary.trade_count == 0
    assert result.trades == []

