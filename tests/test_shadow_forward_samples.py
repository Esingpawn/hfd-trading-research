from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.domain.shadow_forward_samples import (
    candidate_plan_fingerprint,
    candidate_plan_fingerprint_from_trade,
    candidate_snapshot_matches_plan,
    shadow_plan_fingerprint,
    unique_shadow_plans,
)


def trade(
    signal_key: str,
    *,
    pnl: float | None,
    opened_at: datetime,
    closed_at: datetime | None = None,
    fingerprint: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=signal_key,
        strategy_name="darkflow_v2_trade_candidate_shadow_forward_v1",
        symbol="BTCUSDT",
        timeframe="short",
        direction="long",
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=103.0,
        status="closed" if closed_at else "open",
        pnl=pnl,
        opened_at=opened_at,
        closed_at=closed_at,
        context={
            "horizon": "live",
            "shadow_plan_fingerprint": fingerprint,
            "candidate_snapshot": {
                "strategy_id": "pullback_to_cost",
                "timeframe": "short",
                "entry_price": 100.0,
                "stop_price": 99.0,
                "target_price": 103.0,
            },
        },
    )


def test_shadow_plan_fingerprint_prefers_explicit_context_key() -> None:
    item = trade(
        "explicit-signal",
        pnl=0.02,
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        closed_at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        fingerprint="explicit-plan",
    )

    assert shadow_plan_fingerprint(item, include_horizon=True) == "explicit:explicit-plan"


def test_unique_shadow_plans_keeps_best_closed_trade_per_plan() -> None:
    opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        trade("older-loss", pnl=-0.01, opened_at=opened_at, closed_at=opened_at + timedelta(minutes=10), fingerprint="same-plan"),
        trade("newer-win", pnl=0.02, opened_at=opened_at, closed_at=opened_at + timedelta(minutes=30), fingerprint="same-plan"),
        trade("open-same-plan", pnl=None, opened_at=opened_at + timedelta(minutes=40), fingerprint="same-plan"),
    ]

    unique = unique_shadow_plans(rows, include_horizon=True)

    assert [item.id for item in unique] == ["newer-win"]


def test_candidate_plan_fingerprints_match_shadow_trade_candidate_snapshot() -> None:
    item = trade(
        "trade-from-candidate",
        pnl=None,
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        fingerprint=None,
    )
    candidate = SimpleNamespace(
        strategy_id="pullback_to_cost",
        symbol="BTCUSDT",
        timeframe="short",
        direction="long",
        entry_price=100.0,
        stop_price=99.0,
        target_price=103.0,
    )

    assert candidate_plan_fingerprint(candidate) == "pullback_to_cost:BTCUSDT:short:long:100.0:99.00:103.0"
    assert candidate_plan_fingerprint_from_trade(item) == candidate_plan_fingerprint(candidate)
    assert candidate_snapshot_matches_plan(candidate, item.context["candidate_snapshot"])
