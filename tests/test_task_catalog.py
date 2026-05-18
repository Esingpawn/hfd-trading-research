from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.task_catalog import task_catalog_payload, task_spec
from app.application.tasks import run_task_by_id
from app.db import Base
from app.models import TaskRun


def test_task_catalog_classifies_core_legacy_and_infra_tasks() -> None:
    assert task_spec("darkflow.trade_candidates").lineage == "core_darkflow_v2"
    assert task_spec("darkflow.alpha_accelerate").lineage == "core_darkflow_v2"
    assert task_spec("darkflow.alpha_accelerate").production_allowed is True
    assert task_spec("darkflow.waiting_refresh").lineage == "core_darkflow_v2"
    assert task_spec("darkflow.waiting_refresh").production_allowed is True
    assert task_spec("features.research-reports").lineage == "legacy_feature_research"
    assert task_spec("storage-maintain").lineage == "infrastructure_only"
    assert task_spec("features.research-reports").production_allowed is False
    assert task_spec("darkflow.trade_candidates").production_allowed is True
    assert task_spec("missing") is None


def test_task_catalog_payload_is_dashboard_safe() -> None:
    payload = task_catalog_payload()

    assert any(item["canonical_name"] == "darkflow.trade_candidates" for item in payload)
    assert any(item["canonical_name"] == "darkflow.alpha_scoreboard" for item in payload)
    assert any(item["canonical_name"] == "darkflow.alpha_accelerate" for item in payload)
    assert any(item["canonical_name"] == "darkflow.waiting_refresh" for item in payload)
    assert all("lineage" in item and "production_allowed" in item for item in payload)


@pytest.mark.asyncio
async def test_task_result_records_catalog_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", "")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        item = TaskRun(task_name="tasks.reap_stale", payload={"queued_after_seconds": 60}, result={})
        session.add(item)
        await session.commit()

        await run_task_by_id(session, item.id)

        assert item.result["task_catalog"] == {
            "canonical_name": "tasks.reap_stale",
            "lineage": "infrastructure_only",
            "production_allowed": True,
            "heavy": False,
        }
        assert item.finished_at is not None or datetime.now(timezone.utc)
    await engine.dispose()
