from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    BacktestRun,
    CollectionRun,
    PaperTrade,
    PriceSnapshot,
    SignalObservation,
    SignalSnapshot,
    StrategyDecision,
)


MODELS = (
    CollectionRun,
    SignalSnapshot,
    PriceSnapshot,
    StrategyDecision,
    PaperTrade,
    BacktestRun,
    SignalObservation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate HFD data from SQLite to PostgreSQL")
    parser.add_argument("--source-url", default="sqlite+aiosqlite:///./data/hfd.db")
    parser.add_argument("--target-url", default=os.getenv("TARGET_DATABASE_URL"))
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", nargs="*", help="Optional table names to migrate")
    return parser


async def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.target_url:
        raise SystemExit("--target-url or TARGET_DATABASE_URL is required")
    source_engine = create_async_engine(args.source_url)
    target_engine = create_async_engine(args.target_url)
    source_sessionmaker = async_sessionmaker(source_engine, expire_on_commit=False)
    target_sessionmaker = async_sessionmaker(target_engine, expire_on_commit=False)
    selected = set(args.only or [])
    try:
        async with source_sessionmaker() as source, target_sessionmaker() as target:
            for model in MODELS:
                if selected and model.__tablename__ not in selected:
                    continue
                count = await migrate_table(
                    source,
                    target,
                    model,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                )
                print({"table": model.__tablename__, "rows": count, "dry_run": args.dry_run}, flush=True)
    finally:
        await source_engine.dispose()
        await target_engine.dispose()
    return 0


async def migrate_table(source, target, model, *, batch_size: int, dry_run: bool) -> int:
    offset = 0
    migrated = 0
    while True:
        rows = await source.execute(select(model).offset(offset).limit(batch_size))
        items = rows.scalars().all()
        if not items:
            break
        migrated += len(items)
        if not dry_run:
            for item in items:
                await target.merge(_clone_model(item, model))
            await target.commit()
        offset += batch_size
    return migrated


def _clone_model(item, model):
    values = {column.name: getattr(item, column.name) for column in model.__table__.columns}
    return model(**values)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
