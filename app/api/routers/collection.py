from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.api.shared import _completeness_cache_clear, _market_cache_clear
from app.constants import EXPERIMENT_INDICATORS, REQUIRED_SCORING_INDICATORS
from app.services.collector import SnapshotCollector

router = APIRouter()


@router.post("/collect/run")
async def run_collection(
    session: SessionDep,
    dry_run: bool = True,
    coins: list[str] | None = Query(default=None),
    timeframes: list[str] | None = Query(default=None),
    indicators: list[str] | None = Query(default=None),
) -> dict[str, object]:
    collector = SnapshotCollector(session)
    try:
        result = await collector.collect(
            assets=coins,
            timeframes=timeframes,
            indicators=indicators,
            dry_run=dry_run,
        )
        if not dry_run:
            _market_cache_clear()
            _completeness_cache_clear()
        return vars(result)
    finally:
        await collector.close()


@router.post("/collect/scoring-core")
async def collect_scoring_core(
    session: SessionDep,
    dry_run: bool = True,
    coins: list[str] | None = Query(default=None),
    timeframes: list[str] | None = Query(default=None),
) -> dict[str, object]:
    collector = SnapshotCollector(session)
    try:
        result = await collector.collect(
            assets=coins,
            timeframes=timeframes,
            indicators=list(REQUIRED_SCORING_INDICATORS),
            dry_run=dry_run,
        )
        if not dry_run:
            _market_cache_clear()
            _completeness_cache_clear()
        return vars(result)
    finally:
        await collector.close()


@router.post("/collect/experiments")
async def collect_experiments(
    session: SessionDep,
    dry_run: bool = True,
    coins: list[str] | None = Query(default=None),
    timeframes: list[str] | None = Query(default=None),
) -> dict[str, object]:
    collector = SnapshotCollector(session)
    try:
        result = await collector.collect(
            assets=coins,
            timeframes=timeframes,
            indicators=list(EXPERIMENT_INDICATORS),
            dry_run=dry_run,
        )
        if not dry_run:
            _market_cache_clear()
            _completeness_cache_clear()
        payload = vars(result)
        payload["policy"] = {
            "used_for_execution_weights": False,
            "used_for_opening_decisions": False,
            "note": "Experiment collection is research-only and does not change paper/live opening logic.",
        }
        return payload
    finally:
        await collector.close()
