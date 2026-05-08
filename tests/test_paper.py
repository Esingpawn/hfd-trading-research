import pytest

from app.models import StrategyDecision
from app.services.paper import _collection_run_id, _confirmation_for_symbol, _record_paper_scan_status


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


@pytest.mark.asyncio
async def test_confirmation_counts_distinct_collection_runs_only() -> None:
    items = [
        decision(
            {
                "execution_gate": {"ready": True},
                "paper_scan_context": {"collection_run_id": "run-2"},
            }
        ),
        decision(
            {
                "execution_gate": {"ready": True},
                "paper_scan_context": {"collection_run_id": "run-2"},
            }
        ),
        decision(
            {
                "execution_gate": {"ready": True},
                "paper_scan_context": {"collection_run_id": "run-1"},
            }
        ),
    ]

    result = await _confirmation_for_symbol(DecisionSession(items), "ZECUSDT", "short")

    assert result["streak"] == 2
    assert result["confirmed"] is True


@pytest.mark.asyncio
async def test_confirmation_stays_pending_for_duplicate_collection_run() -> None:
    items = [
        decision(
            {
                "execution_gate": {"ready": True},
                "paper_scan_context": {"collection_run_id": "run-2"},
            }
        ),
        decision(
            {
                "execution_gate": {"ready": True},
                "paper_scan_context": {"collection_run_id": "run-2"},
            }
        ),
    ]

    result = await _confirmation_for_symbol(DecisionSession(items), "ZECUSDT", "short")

    assert result["streak"] == 1
    assert result["confirmed"] is False


@pytest.mark.asyncio
async def test_confirmation_ignores_legacy_decisions_without_collection_context() -> None:
    items = [
        decision({"execution_gate": {"ready": True}}),
        decision(
            {
                "execution_gate": {"ready": True},
                "paper_scan_context": {"collection_run_id": "run-1"},
            }
        ),
    ]

    result = await _confirmation_for_symbol(DecisionSession(items), "ZECUSDT", "short")

    assert result["streak"] == 1
    assert result["confirmed"] is False
