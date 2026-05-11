from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.tasks import enqueue_task, recent_tasks, run_task_by_id
from app.api.routers.tasks import _task_enqueue_payload
from app.db import Base
from app.models import ExperimentRun, FeatureEvent, FeatureLabel, SignalSnapshot, TaskRun


@pytest.fixture()
async def session(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_enqueue_task_records_task_without_redis(session) -> None:
    result = await enqueue_task(session, task_name="collect", payload={"coin": "BTC"})
    rows = await recent_tasks(session)

    assert result["status"] == "recorded"
    assert rows[0]["task_name"] == "collect"
    assert rows[0]["payload"] == {"coin": "BTC"}


def test_task_enqueue_payload_preserves_false_research_dedupe_flag() -> None:
    payload = _task_enqueue_payload(
        dedupe_research_samples=False,
        min_unique_event_days=2,
        dry_run=False,
        notify=False,
    )

    assert payload["dedupe_research_samples"] is False
    assert payload["min_unique_event_days"] == 2
    assert "dry_run" not in payload
    assert "notify" not in payload


@pytest.mark.asyncio
async def test_run_task_by_id_executes_storage_maintenance(session) -> None:
    item = TaskRun(task_name="storage.maintain", payload={"indexes": True}, result={})
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)
    rows = await session.execute(select(TaskRun).where(TaskRun.id == item.id))
    stored = rows.scalar_one()

    assert result["status"] == "completed"
    assert stored.status == "completed"
    assert stored.finished_at is not None
    assert stored.result["execution"]["actions"]["indexes"]["status"] == "ok"


@pytest.mark.asyncio
async def test_run_task_by_id_executes_feature_backfill(session) -> None:
    rows = [
        [1_700_000_000_000, 100, 101, 99, 102, 10],
        [1_700_001_800_000, 101, 102, 100, 103, 10],
    ]
    session.add(
        SignalSnapshot(
            symbol="BTCUSDT",
            asset_tier="core",
            timeframe="short",
            interval="30m",
            indicator="inst_choch",
            endpoint="/api/pro/pro_data",
            raw_payload={
                "klines": rows,
                "inst_choch": [{"timestamp": rows[0][0], "price": 101, "type": "CHoCH_Bullish"}],
            },
            summary_payload={},
            collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    item = TaskRun(task_name="features.backfill", payload={"limit": 10}, result={})
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)
    stored_events = await session.execute(select(FeatureEvent))

    assert result["status"] == "completed"
    assert result["result"]["execution"]["events_inserted"] == 1
    assert len(stored_events.scalars().all()) == 1


@pytest.mark.asyncio
async def test_run_task_by_id_executes_feature_reset(session) -> None:
    rows = [
        [1_700_000_000_000, 100, 101, 99, 102, 10],
        [1_700_001_800_000, 101, 102, 100, 103, 10],
    ]
    session.add(
        SignalSnapshot(
            symbol="BTCUSDT",
            asset_tier="core",
            timeframe="short",
            interval="30m",
            indicator="inst_choch",
            endpoint="/api/pro/pro_data",
            raw_payload={
                "klines": rows,
                "inst_choch": [{"timestamp": rows[0][0], "price": 101, "type": "CHoCH_Bullish"}],
            },
            summary_payload={},
            collected_at=datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc),
        )
    )
    backfill = TaskRun(task_name="features.backfill", payload={"limit": 10}, result={})
    session.add(backfill)
    await session.commit()
    await run_task_by_id(session, backfill.id)

    reset = TaskRun(task_name="features.reset", payload={}, result={})
    session.add(reset)
    await session.commit()

    result = await run_task_by_id(session, reset.id)
    stored_events = await session.execute(select(FeatureEvent))

    assert result["status"] == "completed"
    assert result["result"]["execution"]["events_deleted"] == 1
    assert stored_events.scalars().all() == []


@pytest.mark.asyncio
async def test_run_task_by_id_executes_feature_candidate_screen(session) -> None:
    await _add_feature_group(session, symbol="BTCUSDT")
    await _add_feature_group(session, symbol="ETHUSDT")
    item = TaskRun(
        task_name="features.candidates",
        payload={"min_samples": 6, "segment_min_samples": 3, "min_segments": 2, "persist": True},
        result={},
    )
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)
    experiment = await session.scalar(select(ExperimentRun).where(ExperimentRun.name == "feature_candidates_30m"))

    assert result["status"] == "completed"
    assert result["result"]["execution"]["candidate_count"] == 1
    assert experiment is not None
    assert experiment.status == "research"


@pytest.mark.asyncio
async def test_run_task_by_id_executes_feature_paper_ab(session) -> None:
    await _add_feature_group(session, symbol="BTCUSDT")
    await _add_feature_group(session, symbol="ETHUSDT")
    item = TaskRun(
        task_name="features.paper_ab",
        payload={"min_samples": 6, "segment_min_samples": 3, "min_segments": 2, "persist": True},
        result={},
    )
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)
    experiment = await session.scalar(select(ExperimentRun).where(ExperimentRun.name == "feature_paper_ab_30m"))

    assert result["status"] == "completed"
    assert result["result"]["execution"]["policy"]["opens_paper_trades"] is False
    assert result["result"]["execution"]["selected_candidate_count"] == 1
    assert experiment is not None


@pytest.mark.asyncio
async def test_run_task_by_id_honors_feature_candidate_persist_false(session) -> None:
    await _add_feature_group(session, symbol="BTCUSDT")
    await _add_feature_group(session, symbol="ETHUSDT")
    item = TaskRun(
        task_name="features.candidates",
        payload={"min_samples": 6, "segment_min_samples": 3, "min_segments": 2, "persist": False},
        result={},
    )
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)
    experiments = await session.execute(select(ExperimentRun))

    assert result["status"] == "completed"
    assert result["result"]["execution"].get("experiment_run") is None
    assert experiments.scalars().all() == []


@pytest.mark.asyncio
async def test_run_task_by_id_executes_segment_feature_candidates(session) -> None:
    await _add_feature_group(session, symbol="BTCUSDT")
    await _add_feature_group(session, symbol="ZECUSDT", returns=[-0.006, -0.005, -0.004, -0.003, 0.001, -0.002])
    item = TaskRun(
        task_name="features.segment_candidates",
        payload={
            "min_samples": 6,
            "min_win_rate": 0.6,
            "min_profit_factor": 1.2,
            "min_unique_time_buckets": 1,
            "min_unique_event_days": 1,
            "min_unique_market_windows": 1,
            "min_unique_collection_runs": 1,
            "max_same_return_samples": 6,
            "max_return_cluster_ratio": 1.0,
            "persist": True,
        },
        result={},
    )
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)
    experiment = await session.scalar(select(ExperimentRun).where(ExperimentRun.name == "feature_segment_candidates_30m"))

    assert result["status"] == "completed"
    assert result["result"]["execution"]["candidate_count"] == 1
    assert result["result"]["execution"]["candidates"][0]["symbol"] == "BTCUSDT"
    assert result["result"]["execution"]["candidates"][0]["quality"]["dedupe_research_samples"] is True
    assert experiment is not None


@pytest.mark.asyncio
async def test_run_task_by_id_executes_segment_feature_paper_ab(session) -> None:
    await _add_feature_group(session, symbol="BTCUSDT")
    await _add_feature_group(session, symbol="ZECUSDT", returns=[-0.006, -0.005, -0.004, -0.003, 0.001, -0.002])
    item = TaskRun(
        task_name="features.segment_paper_ab",
        payload={
            "min_samples": 6,
            "min_win_rate": 0.6,
            "min_profit_factor": 1.2,
            "min_unique_time_buckets": 1,
            "min_unique_event_days": 1,
            "min_unique_market_windows": 1,
            "min_unique_collection_runs": 1,
            "max_same_return_samples": 6,
            "max_return_cluster_ratio": 1.0,
            "persist": True,
        },
        result={},
    )
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)
    experiment = await session.scalar(select(ExperimentRun).where(ExperimentRun.name == "feature_segment_paper_ab_30m"))

    assert result["status"] == "completed"
    assert result["result"]["execution"]["selected_candidate_count"] == 1
    assert result["result"]["execution"]["policy"]["opens_paper_trades"] is False
    assert result["result"]["execution"]["quality"]["candidate"]["raw_sample_count"] >= 1
    assert experiment is not None


@pytest.mark.asyncio
async def test_run_task_by_id_records_failure(session) -> None:
    item = TaskRun(task_name="unknown.task", payload={}, result={})
    session.add(item)
    await session.commit()

    with pytest.raises(ValueError):
        await run_task_by_id(session, item.id)

    rows = await session.execute(select(TaskRun).where(TaskRun.id == item.id))
    stored = rows.scalar_one()
    assert stored.status == "failed"
    assert "unsupported task_name" in str(stored.error)


async def _add_feature_group(session, *, symbol: str, returns: list[float] | None = None) -> None:
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, return_pct in enumerate(returns or [0.012, 0.01, 0.009, 0.008, 0.011, -0.002]):
        event = FeatureEvent(
            snapshot_id=f"snapshot-{symbol}-{index}",
            symbol=symbol,
            asset_tier="core",
            timeframe="short",
            interval="30m",
            indicator="inst_choch",
            event_key=f"event-{symbol}-{index}",
            feature_name="inst_choch",
            direction="long",
            event_ts=base_ts + timedelta(minutes=index * 31),
            event_price=100.0,
            strength=0.7,
            subtype="CHoCH_Bullish",
            source_payload_key="inst_choch",
            context={},
        )
        session.add(event)
        await session.flush()
        session.add(
            FeatureLabel(
                feature_event_id=event.id,
                horizon="30m",
                return_pct=return_pct,
                mfe=max(return_pct, 0.0),
                mae=min(return_pct, 0.0),
                future_price=100.0 * (1 + return_pct),
                future_at=base_ts,
                status="labeled",
            )
        )
