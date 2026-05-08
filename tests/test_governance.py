import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.governance import (
    create_experiment_run,
    create_weight_version,
    list_experiment_runs,
    list_weight_versions,
)
from app.db import Base


@pytest.fixture()
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'governance.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_experiment_and_weight_versions(session) -> None:
    experiment = await create_experiment_run(session, name="liquidity_sweep_v1", scope={"symbol": "BTCUSDT"})
    weight = await create_weight_version(session, name="baseline", weights={"direction": 1.0})

    experiments = await list_experiment_runs(session)
    weights = await list_weight_versions(session)

    assert experiment["name"] == "liquidity_sweep_v1"
    assert experiments[0]["scope"] == {"symbol": "BTCUSDT"}
    assert weight["weights"] == {"direction": 1.0}
    assert weights[0]["name"] == "baseline"
