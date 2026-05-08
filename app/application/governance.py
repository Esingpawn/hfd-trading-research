from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExperimentRun, WeightVersion


async def create_experiment_run(
    session: AsyncSession,
    *,
    name: str,
    scope: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    item = ExperimentRun(name=name, scope=scope or {}, params=params or {}, metrics={}, notes=notes)
    session.add(item)
    await session.commit()
    return _experiment_payload(item)


async def list_experiment_runs(session: AsyncSession, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = await session.execute(select(ExperimentRun).order_by(ExperimentRun.created_at.desc()).limit(limit))
    return [_experiment_payload(item) for item in rows.scalars()]


async def create_weight_version(
    session: AsyncSession,
    *,
    name: str,
    weights: dict[str, Any],
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = WeightVersion(name=name, weights=weights, evidence=evidence or {})
    session.add(item)
    await session.commit()
    return _weight_payload(item)


async def list_weight_versions(session: AsyncSession, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = await session.execute(select(WeightVersion).order_by(WeightVersion.created_at.desc()).limit(limit))
    return [_weight_payload(item) for item in rows.scalars()]


def _experiment_payload(item: ExperimentRun) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "status": item.status,
        "scope": item.scope,
        "params": item.params,
        "metrics": item.metrics,
        "notes": item.notes,
        "created_at": item.created_at,
    }


def _weight_payload(item: WeightVersion) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "status": item.status,
        "weights": item.weights,
        "evidence": item.evidence,
        "activated_at": item.activated_at,
        "created_at": item.created_at,
    }
