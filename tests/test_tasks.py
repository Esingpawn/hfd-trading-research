from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.tasks import enqueue_task, reap_stale_tasks, recent_tasks, run_task_by_id
from app.api.routers.signals import refresh_research_reports
from app.api.routers.tasks import _task_enqueue_payload
from app.db import Base
from app.models import ExperimentRun, FeatureEvent, FeatureLabel, PaperTrade, PriceSnapshot, ShadowPaperTrade, SignalSnapshot, TaskRun


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


@pytest.mark.asyncio
async def test_enqueue_task_commits_record_before_queue_publish(session, monkeypatch) -> None:
    seen: list[TaskRun | None] = []

    class InspectingQueue:
        async def enqueue(self, name: str, payload: dict) -> dict[str, object]:
            seen.append(await session.get(TaskRun, payload["task_run_id"]))
            return {"status": "queued", "queue": "test", "task": name, "length": 1}

    monkeypatch.setattr("app.application.tasks.build_queue", lambda: InspectingQueue())

    result = await enqueue_task(session, task_name="collect", payload={"coin": "BTC"})

    assert result["status"] == "queued"
    assert seen and seen[0] is not None
    assert seen[0].task_name == "collect"


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


def test_task_enqueue_payload_keeps_research_acceleration_limits() -> None:
    payload = _task_enqueue_payload(
        task_name="research.accelerate",
        feature_limit=20,
        label_limit=50,
        signal_limit=30,
        report_limit=100,
        candidate_limit=5,
    )

    assert payload["feature_limit"] == 20
    assert payload["label_limit"] == 50
    assert payload["signal_limit"] == 30
    assert payload["report_limit"] == 100
    assert payload["candidate_limit"] == 5


def test_task_enqueue_payload_keeps_stale_reaper_limits() -> None:
    payload = _task_enqueue_payload(
        task_name="tasks.reap_stale",
        queued_after_seconds=120,
        running_after_seconds=240,
    )

    assert payload["queued_after_seconds"] == 120
    assert payload["running_after_seconds"] == 240


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
async def test_run_task_by_id_skips_terminal_task(session) -> None:
    item = TaskRun(
        task_name="storage.maintain",
        status="completed",
        payload={"indexes": True},
        result={"execution": {"status": "already_done"}},
        finished_at=datetime.now(timezone.utc),
    )
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)

    assert result["skipped"] is True
    assert result["skip_reason"] == "task_already_terminal"


@pytest.mark.asyncio
async def test_reap_stale_tasks_marks_old_active_tasks_failed(session) -> None:
    now = datetime.now(timezone.utc)
    stale_running = TaskRun(
        task_name="features.research_reports",
        status="running",
        payload={},
        result={},
        queued_at=now - timedelta(hours=2),
        started_at=now - timedelta(hours=2),
    )
    stale_queued = TaskRun(
        task_name="research.accelerate",
        status="queued",
        payload={},
        result={},
        queued_at=now - timedelta(hours=2),
    )
    fresh_running = TaskRun(
        task_name="research.accelerate",
        status="running",
        payload={},
        result={},
        queued_at=now,
        started_at=now,
    )
    session.add_all([stale_running, stale_queued, fresh_running])
    await session.commit()

    result = await reap_stale_tasks(session, queued_after_seconds=60, running_after_seconds=60)

    assert result["reaped_count"] == 2
    assert stale_running.status == "failed"
    assert stale_queued.status == "failed"
    assert fresh_running.status == "running"
    assert "stale task reaped" in str(stale_running.error)
    assert stale_running.result["stale_reaper"]["previous_status"] == "running"


@pytest.mark.asyncio
async def test_run_task_by_id_executes_stale_reaper_task(session) -> None:
    old = TaskRun(
        task_name="research.accelerate",
        status="running",
        payload={},
        result={},
        queued_at=datetime.now(timezone.utc) - timedelta(hours=2),
        started_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    item = TaskRun(
        task_name="tasks.reap_stale",
        payload={"queued_after_seconds": 60, "running_after_seconds": 60},
        result={},
    )
    session.add_all([old, item])
    await session.commit()

    result = await run_task_by_id(session, item.id)

    assert result["status"] == "completed"
    assert result["result"]["execution"]["reaped_count"] == 1
    assert old.status == "failed"


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
async def test_run_task_by_id_executes_research_report_materialization(session) -> None:
    await _add_feature_group(session, symbol="BTCUSDT")
    await _add_feature_group(session, symbol="ETHUSDT")
    item = TaskRun(
        task_name="features.research_reports",
        payload={"min_samples": 6, "limit": 100},
        result={},
    )
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)
    experiments = await session.execute(select(ExperimentRun))

    assert result["status"] == "completed"
    assert result["result"]["execution"]["generated_count"] == 4
    assert {item.name for item in experiments.scalars().all()} == {
        "feature_candidates_30m",
        "feature_paper_ab_30m",
        "feature_segment_candidates_30m",
        "feature_segment_paper_ab_30m",
    }


@pytest.mark.asyncio
async def test_run_task_by_id_caps_research_report_materialization_limit(session) -> None:
    await _add_feature_group(session, symbol="BTCUSDT")
    item = TaskRun(
        task_name="features.research_reports",
        payload={"min_samples": 6, "limit": 999999},
        result={},
    )
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)

    execution = result["result"]["execution"]
    assert execution["requested_limit"] == 999999
    assert execution["limit"] == 5000
    assert execution["reports"]["feature_candidates"]["limit_capped"] is True


@pytest.mark.asyncio
async def test_run_task_by_id_forces_research_report_materialization(session) -> None:
    await _add_feature_group(session, symbol="BTCUSDT")
    first = TaskRun(
        task_name="features.research_reports",
        payload={"min_samples": 6, "limit": 100},
        result={},
    )
    session.add(first)
    await session.commit()
    await run_task_by_id(session, first.id)
    second = TaskRun(
        task_name="features.research_reports",
        payload={"min_samples": 6, "limit": 100, "force": True},
        result={},
    )
    session.add(second)
    await session.commit()

    result = await run_task_by_id(session, second.id)

    assert result["result"]["execution"]["generated_count"] == 4
    assert result["result"]["execution"].get("skip_reason") is None


@pytest.mark.asyncio
async def test_refresh_research_reports_enqueues_capped_background_task(session) -> None:
    result = await refresh_research_reports(session, horizon="30m", min_samples=30, limit=999999)
    item = await session.get(TaskRun, result["task_run_id"])

    assert result["status"] == "recorded"
    assert result["requested_limit"] == 999999
    assert result["limit"] == 5000
    assert result["limit_capped"] is True
    assert item is not None
    assert item.task_name == "features.research_reports"
    assert item.payload == {"horizon": "30m", "min_samples": 30, "limit": 999999, "force": True}


@pytest.mark.asyncio
async def test_refresh_research_reports_reuses_active_task(session) -> None:
    existing = TaskRun(
        task_name="features.research_reports",
        status="queued",
        payload={"horizon": "30m", "min_samples": 30, "limit": 5000},
        result={},
    )
    session.add(existing)
    await session.commit()

    result = await refresh_research_reports(session, horizon="30m", min_samples=30, limit=100000)
    tasks = await session.execute(select(TaskRun).where(TaskRun.task_name == "features.research_reports"))

    assert result["status"] == "already_running"
    assert result["task_run_id"] == existing.id
    assert result["limit"] == 5000
    assert len(tasks.scalars().all()) == 1


@pytest.mark.asyncio
async def test_refresh_research_reports_ignores_stale_queued_task(session) -> None:
    existing = TaskRun(
        task_name="features.research_reports",
        status="queued",
        payload={"horizon": "30m", "min_samples": 30, "limit": 5000},
        result={},
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    session.add(existing)
    await session.commit()

    result = await refresh_research_reports(session, horizon="30m", min_samples=30, limit=100000)
    tasks = await session.execute(select(TaskRun).where(TaskRun.task_name == "features.research_reports"))

    assert result["status"] == "recorded"
    assert result["task_run_id"] != existing.id
    assert len(tasks.scalars().all()) == 2


@pytest.mark.asyncio
async def test_refresh_research_reports_ignores_stale_running_task(session) -> None:
    existing = TaskRun(
        task_name="features.research_reports",
        status="running",
        payload={"horizon": "30m", "min_samples": 30, "limit": 5000},
        result={},
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=15),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=15),
    )
    session.add(existing)
    await session.commit()

    result = await refresh_research_reports(session, horizon="30m", min_samples=30, limit=100000)
    tasks = await session.execute(select(TaskRun).where(TaskRun.task_name == "features.research_reports"))

    assert result["status"] == "recorded"
    assert result["task_run_id"] != existing.id
    assert len(tasks.scalars().all()) == 2


@pytest.mark.asyncio
async def test_run_task_by_id_executes_data_quality_report(session) -> None:
    item = TaskRun(task_name="data_quality.report", payload={}, result={})
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)

    assert result["status"] == "completed"
    assert result["result"]["execution"]["policy"]["read_only"] is True
    assert result["result"]["execution"]["policy"]["opens_live_orders"] is False


@pytest.mark.asyncio
async def test_run_task_by_id_executes_shadow_paper_scan_without_real_paper_trade(session) -> None:
    await _add_feature_group(session, symbol="BTCUSDT")
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    materialize = TaskRun(task_name="features.research_reports", payload={"min_samples": 6, "limit": 100}, result={})
    session.add(materialize)
    await session.commit()
    await run_task_by_id(session, materialize.id)
    scan = TaskRun(task_name="shadow_paper.scan", payload={"candidate_limit": 5}, result={})
    session.add(scan)
    await session.commit()

    result = await run_task_by_id(session, scan.id)
    shadow_rows = await session.execute(select(ShadowPaperTrade))

    assert result["status"] == "completed"
    assert result["result"]["execution"]["policy"]["opens_paper_trades"] is False
    shadow_trades = shadow_rows.scalars().all()
    assert len(shadow_trades) >= 1
    assert all(item.status == "open" for item in shadow_trades)


@pytest.mark.asyncio
async def test_run_task_by_id_executes_shadow_paper_replay_without_real_paper_trade(session) -> None:
    await _add_feature_group(session, symbol="HYPEUSDT")
    materialize = TaskRun(task_name="features.research_reports", payload={"min_samples": 6, "limit": 100}, result={})
    session.add(materialize)
    await session.commit()
    await run_task_by_id(session, materialize.id)
    replay = TaskRun(task_name="shadow_paper.replay", payload={"limit": 10, "candidate_limit": 5}, result={})
    session.add(replay)
    await session.commit()

    result = await run_task_by_id(session, replay.id)
    shadow_rows = await session.execute(select(ShadowPaperTrade))
    paper_rows = await session.execute(select(PaperTrade))

    assert result["status"] == "completed"
    assert result["result"]["execution"]["policy"]["opens_live_orders"] is False
    assert result["result"]["execution"]["policy"]["opens_paper_trades"] is False
    assert result["result"]["execution"]["inserted"] >= 1
    assert all(item.status == "closed" for item in shadow_rows.scalars().all())
    assert paper_rows.scalars().all() == []


@pytest.mark.asyncio
async def test_run_task_by_id_executes_research_acceleration_cycle(session) -> None:
    await _add_feature_group(session, symbol="BTCUSDT")
    await _add_feature_group(session, symbol="ETHUSDT")
    session.add(PriceSnapshot(symbol="BTCUSDT", price=100.0, raw_payload={}, collected_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    item = TaskRun(
        task_name="research.accelerate",
        payload={
            "horizon": "30m",
            "feature_limit": 20,
            "label_limit": 20,
            "report_limit": 100,
            "candidate_limit": 5,
            "min_samples": 6,
        },
        result={},
    )
    session.add(item)
    await session.commit()

    result = await run_task_by_id(session, item.id)
    shadow_rows = await session.execute(select(ShadowPaperTrade))

    execution = result["result"]["execution"]
    assert execution["policy"]["opens_live_orders"] is False
    assert execution["steps"]["research_reports"]["generated_count"] == 4
    assert execution["steps"]["shadow_replay"]["policy"]["opens_paper_trades"] is False
    assert execution["steps"]["shadow_scan"]["policy"]["opens_paper_trades"] is False
    assert execution["steps"]["shadow_promotion"]["promotion"]["criteria"]["cost_model_required"] is True
    shadow_trades = shadow_rows.scalars().all()
    assert len(shadow_trades) >= 1
    assert any(item.context.get("historical_replay") for item in shadow_trades)


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
