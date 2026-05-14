from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import PaperTrade, PriceSnapshot, StrategyDecision
from app.api.shared import _confirmation_from_items
from app.services.paper import _collection_run_id, _confirmation_for_symbol, _exit_reason, mark_open_trades, _record_paper_scan_status


def risk_payload(
    *,
    entry: float = 100.0,
    lower: float = 99.5,
    upper: float = 100.5,
    stop: float = 98.0,
    target: float = 104.0,
    run_id: str = "run-1",
) -> dict:
    return {
        "entry_price": entry,
        "stop_loss": stop,
        "take_profit": target,
        "execution_gate": {"ready": True},
        "execution_zone": {"valid": True, "lower": lower, "upper": upper},
        "entry_plan": {
            "entry_reference_price": entry,
            "entry_lower": lower,
            "entry_upper": upper,
            "stop_loss": stop,
            "take_profit": target,
            "valid_until": "2099-01-01T00:00:00+00:00",
            "drift_limit_pct": 0.003,
        },
        "paper_scan_context": {"collection_run_id": run_id},
    }


class DummySession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class ScalarRows:
    def __init__(self, items: list[StrategyDecision]) -> None:
        self.items = items

    def scalars(self):
        return self

    def all(self) -> list[StrategyDecision]:
        return self.items


class DecisionSession(DummySession):
    def __init__(self, items: list[StrategyDecision]) -> None:
        super().__init__()
        self.items = items

    async def execute(self, _statement):
        return ScalarRows(self.items)


@pytest.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def decision(
    risk_payload: dict | None = None,
    *,
    symbol: str = "ZECUSDT",
    direction: str = "short",
    decision_value: str = "open",
) -> StrategyDecision:
    return StrategyDecision(
        strategy_name="strategy",
        strategy_version="v1",
        symbol=symbol,
        asset_tier="high_volatility",
        direction=direction,
        score=100.0,
        decision=decision_value,
        reason={},
        risk_payload=risk_payload or {"entry_price": 100.0},
    )


def paper_trade(
    *,
    symbol: str = "BTCUSDT",
    asset_tier: str = "core",
    direction: str = "long",
    entry: float = 100.0,
    stop: float = 98.0,
    target: float = 104.0,
) -> PaperTrade:
    return PaperTrade(
        strategy_decision_id="decision-1",
        strategy_name="strategy",
        strategy_version="v1",
        symbol=symbol,
        asset_tier=asset_tier,
        direction=direction,
        entry_price=entry,
        stop_loss=stop,
        take_profit=target,
        position_size=1.0,
    )


def runner_decision(
    *,
    decision_id: str,
    symbol: str,
    direction: str,
    score: float,
    created_at: datetime | None = None,
) -> StrategyDecision:
    return StrategyDecision(
        id=decision_id,
        strategy_name="strategy",
        strategy_version="v1",
        symbol=symbol,
        asset_tier="high_volatility",
        direction=direction,
        score=score,
        decision="open",
        reason={
            "rules": [
                "long_term_direction",
                "mid_term_aligned",
                "short_term_aligned",
                "liquidity_context_present",
                "orderflow_present",
                "exhaustion_present",
            ]
        },
        risk_payload={
            "min_score": 82,
            "target_source": "liq_heatmap.heatmap_data",
            "execution_gate": {"ready": True},
        },
        created_at=created_at or datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_record_paper_scan_status_updates_decision_payload() -> None:
    session = DummySession()
    item = decision()
    confirmation = {"required": 2, "streak": 1, "confirmed": False}

    await _record_paper_scan_status(
        session,
        item,
        confirmation=confirmation,
        status="awaiting_confirmation",
        scan_context={"collection_run_id": "run-1"},
    )

    assert item.risk_payload["entry_price"] == 100.0
    assert item.risk_payload["confirmation"] == confirmation
    assert item.risk_payload["paper_scan_status"] == "awaiting_confirmation"
    assert item.risk_payload["paper_scan_context"]["collection_run_id"] == "run-1"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_record_paper_scan_status_can_defer_commit() -> None:
    session = DummySession()
    item = decision()

    await _record_paper_scan_status(
        session,
        item,
        confirmation={"required": 2, "streak": 2, "confirmed": True},
        status="opened",
        commit=False,
    )

    assert item.risk_payload["paper_scan_status"] == "opened"
    assert session.commits == 0


def test_collection_run_id_reads_scan_context() -> None:
    item = decision({"paper_scan_context": {"collection_run_id": "run-2"}})

    assert _collection_run_id(item) == "run-2"


def test_core_trade_still_exits_at_take_profit() -> None:
    trade = paper_trade(symbol="BTCUSDT", asset_tier="core", entry=100.0, stop=98.0, target=104.0)

    assert _exit_reason(trade, 104.5) == "take_profit"
    assert trade.stop_loss == 98.0
    assert trade.take_profit == 104.0


def test_high_volatility_trade_without_evidence_still_reaches_take_profit() -> None:
    trade = paper_trade(symbol="HYPEUSDT", asset_tier="high_volatility", entry=38.0, stop=37.05, target=39.52)

    assert _exit_reason(trade, 39.6) == "take_profit"
    assert trade.stop_loss == 37.05
    assert trade.take_profit == 39.52


def test_high_volatility_runner_exits_on_trailing_stop() -> None:
    trade = paper_trade(symbol="HYPEUSDT", asset_tier="high_volatility", entry=38.0, stop=37.05, target=39.52)
    trade.stop_loss = 38.72
    trailing_stop = trade.stop_loss

    assert _exit_reason(trade, trailing_stop) == "trailing_stop"


@pytest.mark.asyncio
async def test_mark_open_trades_extends_hype_runner(db_session) -> None:
    db_session.add(
        runner_decision(
            decision_id="decision-1",
            symbol="HYPEUSDT",
            direction="long",
            score=91.0,
        )
    )
    db_session.add(
        paper_trade(symbol="HYPEUSDT", asset_tier="high_volatility", entry=38.0, stop=37.05, target=39.52)
    )
    db_session.add(
        PriceSnapshot(
            symbol="HYPEUSDT",
            price=39.6,
            raw_payload={},
            collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    await db_session.commit()

    result = await mark_open_trades(db_session)
    stored = await db_session.scalar(select(PaperTrade).where(PaperTrade.symbol == "HYPEUSDT"))

    assert result["closed"] == []
    assert result["updated"][0]["runner_extended"] is True
    assert stored.status == "open"
    assert stored.stop_loss > 38.0
    assert stored.take_profit > 39.6


@pytest.mark.asyncio
async def test_mark_open_trades_can_use_opening_runner_evidence_for_hype(db_session) -> None:
    opened_at = datetime.now(timezone.utc)
    db_session.add(
        runner_decision(
            decision_id="decision-1",
            symbol="HYPEUSDT",
            direction="long",
            score=91.0,
            created_at=opened_at - timedelta(hours=3),
        )
    )
    trade = paper_trade(symbol="HYPEUSDT", asset_tier="high_volatility", entry=38.0, stop=37.05, target=39.52)
    trade.opened_at = opened_at
    db_session.add(trade)
    db_session.add(
        PriceSnapshot(
            symbol="HYPEUSDT",
            price=39.6,
            raw_payload={},
            collected_at=opened_at,
        )
    )
    await db_session.commit()

    result = await mark_open_trades(db_session)
    stored = await db_session.scalar(select(PaperTrade).where(PaperTrade.symbol == "HYPEUSDT"))

    assert result["closed"] == []
    assert result["updated"][0]["runner_extended"] is True
    assert result["updated"][0]["runner_evidence"]["signals"]["opening_evidence_fallback"] is True
    assert stored.status == "open"
    assert stored.stop_loss > 38.0
    assert stored.take_profit > 39.6


@pytest.mark.asyncio
async def test_mark_open_trades_does_not_use_expired_opening_runner_evidence(db_session) -> None:
    opened_at = datetime.now(timezone.utc) - timedelta(days=2)
    db_session.add(
        runner_decision(
            decision_id="decision-1",
            symbol="HYPEUSDT",
            direction="long",
            score=91.0,
            created_at=opened_at,
        )
    )
    trade = paper_trade(symbol="HYPEUSDT", asset_tier="high_volatility", entry=38.0, stop=37.05, target=39.52)
    trade.opened_at = opened_at
    db_session.add(trade)
    db_session.add(
        PriceSnapshot(
            symbol="HYPEUSDT",
            price=39.6,
            raw_payload={},
            collected_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    result = await mark_open_trades(db_session)
    stored = await db_session.scalar(select(PaperTrade).where(PaperTrade.symbol == "HYPEUSDT"))

    assert result["updated"] == []
    assert result["closed"][0]["reason"] == "take_profit"
    assert stored.status == "closed"
    assert stored.exit_reason == "take_profit"


@pytest.mark.asyncio
async def test_mark_open_trades_closes_take_profit_without_runner_evidence(db_session) -> None:
    db_session.add(
        runner_decision(
            decision_id="decision-1",
            symbol="HYPEUSDT",
            direction="short",
            score=91.0,
        )
    )
    db_session.add(
        paper_trade(symbol="HYPEUSDT", asset_tier="high_volatility", entry=38.0, stop=37.05, target=39.52)
    )
    db_session.add(
        PriceSnapshot(
            symbol="HYPEUSDT",
            price=39.6,
            raw_payload={},
            collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    await db_session.commit()

    result = await mark_open_trades(db_session)
    stored = await db_session.scalar(select(PaperTrade).where(PaperTrade.symbol == "HYPEUSDT"))

    assert result["updated"] == []
    assert result["closed"][0]["reason"] == "take_profit"
    assert stored.status == "closed"
    assert stored.exit_reason == "take_profit"


@pytest.mark.asyncio
async def test_confirmation_counts_distinct_collection_runs_only() -> None:
    items = [
        decision(risk_payload(run_id="run-2")),
        decision(risk_payload(run_id="run-2")),
        decision(risk_payload(run_id="run-1")),
    ]

    result = await _confirmation_for_symbol(DecisionSession(items), "ZECUSDT", "short", current_risk=risk_payload(run_id="run-3"))

    assert result["streak"] == 2
    assert result["confirmed"] is True
    assert result["plan_compatible"] is True


@pytest.mark.asyncio
async def test_confirmation_stays_pending_for_duplicate_collection_run() -> None:
    items = [
        decision(risk_payload(run_id="run-2")),
        decision(risk_payload(run_id="run-2")),
    ]

    result = await _confirmation_for_symbol(DecisionSession(items), "ZECUSDT", "short", current_risk=risk_payload(run_id="run-3"))

    assert result["streak"] == 1
    assert result["confirmed"] is False


@pytest.mark.asyncio
async def test_confirmation_rejects_shifted_entry_plan() -> None:
    items = [
        decision(risk_payload(entry=100.0, lower=99.5, upper=100.5, run_id="run-2")),
        decision(risk_payload(entry=100.0, lower=99.5, upper=100.5, run_id="run-1")),
    ]

    result = await _confirmation_for_symbol(
        DecisionSession(items),
        "ZECUSDT",
        "short",
        current_risk=risk_payload(entry=101.0, lower=100.5, upper=101.5, run_id="run-3"),
    )

    assert result["streak"] == 0
    assert result["confirmed"] is False
    assert result["plan_compatible"] is False
    assert "entry_reference_drift" in result["plan_checks"][0]["reasons"]


def test_market_confirmation_rejects_shifted_entry_plan() -> None:
    items = [decision(risk_payload(entry=100.0, run_id="run-2")), decision(risk_payload(entry=100.0, run_id="run-1"))]

    result = _confirmation_from_items(items, "short", current_risk=risk_payload(entry=101.0, run_id="run-3"))

    assert result["streak"] == 0
    assert result["confirmed"] is False
    assert result["plan_checks"][0]["compatible"] is False


@pytest.mark.asyncio
async def test_confirmation_ignores_legacy_decisions_without_collection_context() -> None:
    items = [
        decision({"execution_gate": {"ready": True}}),
        decision(risk_payload(run_id="run-1")),
    ]

    result = await _confirmation_for_symbol(DecisionSession(items), "ZECUSDT", "short", current_risk=risk_payload(run_id="run-2"))

    assert result["streak"] == 1
    assert result["confirmed"] is False
