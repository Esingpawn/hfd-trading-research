from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.application.governance import (
    create_experiment_run,
    create_weight_version,
    list_experiment_runs,
    list_weight_versions,
)

router = APIRouter()


@router.get("/governance/experiments")
async def governance_experiments(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, object]]:
    return await list_experiment_runs(session, limit=limit)


@router.post("/governance/experiments")
async def create_governance_experiment(
    session: SessionDep,
    name: str = Query(..., min_length=1),
) -> dict[str, object]:
    return await create_experiment_run(session, name=name)


@router.get("/governance/weights")
async def governance_weights(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, object]]:
    return await list_weight_versions(session, limit=limit)


@router.post("/governance/weights")
async def create_governance_weight(
    session: SessionDep,
    name: str = Query(..., min_length=1),
) -> dict[str, object]:
    return await create_weight_version(session, name=name, weights={}, evidence={})
