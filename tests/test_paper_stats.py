from datetime import datetime, timezone

from app.models import PaperTrade
from app.services.paper_stats import summarize_paper_trades


def trade(
    status: str,
    pnl: float | None = None,
    r_multiple: float | None = None,
    symbol: str = "BTCUSDT",
    direction: str = "long",
    tier: str = "core",
    mfe: float = 0.0,
    mae: float = 0.0,
) -> PaperTrade:
    return PaperTrade(
        strategy_decision_id="decision",
        strategy_name="strategy",
        strategy_version="v1",
        symbol=symbol,
        asset_tier=tier,
        direction=direction,
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=102.0,
        position_size=0.01,
        status=status,
        pnl=pnl,
        r_multiple=r_multiple,
        mfe=mfe,
        mae=mae,
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_summarize_empty_paper_trades() -> None:
    stats = summarize_paper_trades([])

    assert stats["total_trades"] == 0
    assert stats["win_rate"] is None
    assert stats["sample_ready"] is False


def test_summarize_closed_paper_trades() -> None:
    stats = summarize_paper_trades(
        [
            trade("closed", pnl=0.02, r_multiple=2.0),
            trade("closed", pnl=-0.01, r_multiple=-1.0),
            trade("open"),
        ]
    )

    assert stats["total_trades"] == 3
    assert stats["open_trades"] == 1
    assert stats["closed_trades"] == 2
    assert stats["win_rate"] == 0.5
    assert round(stats["profit_factor"], 4) == 2.0
    assert stats["avg_r_multiple"] == 0.5


def test_summarize_closed_paper_trades_keeps_missing_pnl_out_of_performance() -> None:
    stats = summarize_paper_trades(
        [
            trade("closed", pnl=0.02, r_multiple=2.0),
            trade("closed", pnl=None, r_multiple=None),
        ]
    )

    assert stats["closed_trades"] == 2
    assert stats["valid_outcome_trades"] == 1
    assert stats["invalid_outcome_trades"] == 1
    assert stats["avg_pnl"] == 0.02
    assert stats["total_pnl"] == 0.02
    assert stats["win_rate"] == 1.0


def test_summarize_groups_by_symbol_direction_and_tier() -> None:
    stats = summarize_paper_trades(
        [
            trade("closed", pnl=0.02, r_multiple=2.0, symbol="BTCUSDT", direction="long", tier="core"),
            trade("closed", pnl=-0.01, r_multiple=-1.0, symbol="BTCUSDT", direction="short", tier="core"),
            trade("closed", pnl=0.03, r_multiple=1.5, symbol="ETHUSDT", direction="long", tier="core"),
            trade("open", symbol="DOGEUSDT", direction="short", tier="high_volatility", mfe=0.04, mae=-0.02),
        ]
    )

    by_symbol = {row["key"]: row for row in stats["by_symbol"]}
    by_direction = {row["key"]: row for row in stats["by_direction"]}
    by_tier = {row["key"]: row for row in stats["by_tier"]}

    assert by_symbol["BTCUSDT"]["closed_trades"] == 2
    assert by_symbol["ETHUSDT"]["win_rate"] == 1.0
    assert by_direction["long"]["win_count"] == 2
    assert by_tier["high_volatility"]["open_trades"] == 1
    assert stats["open_exposure"][0]["symbol"] == "DOGEUSDT"
    assert stats["open_mae"] == -0.02
    assert stats["sample_progress"] == 0.015
