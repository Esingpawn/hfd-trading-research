from app.services.features import summarize_signal_payload


def test_summarize_signal_payload_extracts_kline_bounds() -> None:
    payload = {
        "klines": [
            [1000, 10, 11, 9, 12, 100],
            [2000, 11, 12, 10, 13, 120],
        ],
        "smart_money_cost": [{"avg_price": 11.5}],
    }

    summary = summarize_signal_payload(payload, "smart_money_cost")

    assert summary["kline_count"] == 2
    assert summary["first_kline_ts"] == 1000
    assert summary["last_kline_ts"] == 2000
    assert summary["last_close"] == 12
    assert summary["indicator_item_count"] == 1

