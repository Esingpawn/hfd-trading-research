from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.infrastructure.raw_store import LocalRawPayloadStore
from app.models import (
    BacktestRun,
    CollectionRun,
    PaperTrade,
    PriceSnapshot,
    SignalObservation,
    SignalSnapshot,
    StrategyDecision,
    TradeOrder,
    TradingAuditLog,
    TradingSafetyState,
)


MODELS = (
    CollectionRun,
    SignalSnapshot,
    PriceSnapshot,
    StrategyDecision,
    PaperTrade,
    BacktestRun,
    SignalObservation,
    TradingSafetyState,
    TradeOrder,
    TradingAuditLog,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate HFD data from SQLite to PostgreSQL")
    parser.add_argument("--source-url", default="sqlite+aiosqlite:///./data/hfd.db")
    parser.add_argument("--target-url", default=os.getenv("TARGET_DATABASE_URL"))
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", nargs="*", help="Optional table names to migrate")
    parser.add_argument(
        "--externalize-raw-payloads",
        action="store_true",
        help="Move legacy signal_snapshots.raw_payload JSON into gzip files during migration.",
    )
    parser.add_argument(
        "--raw-payload-dir",
        default=os.getenv("RAW_PAYLOAD_DIR", "./data/raw_payloads"),
        help="Directory used when --externalize-raw-payloads is enabled.",
    )
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
            source_tables = await _table_names(source_engine)
            raw_store = LocalRawPayloadStore(args.raw_payload_dir)
            for model in MODELS:
                if selected and model.__tablename__ not in selected:
                    continue
                if model.__tablename__ not in source_tables:
                    print({"table": model.__tablename__, "rows": 0, "skipped": "missing_source_table", "dry_run": args.dry_run}, flush=True)
                    continue
                source_columns = await _column_names(source_engine, model.__tablename__)
                count = await migrate_table(
                    source,
                    target,
                    model,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                    raw_store=raw_store if args.externalize_raw_payloads else None,
                    source_columns=source_columns,
                )
                print({"table": model.__tablename__, "rows": count, "dry_run": args.dry_run}, flush=True)
    finally:
        await source_engine.dispose()
        await target_engine.dispose()
    return 0


async def migrate_table(
    source,
    target,
    model,
    *,
    batch_size: int,
    dry_run: bool,
    raw_store: LocalRawPayloadStore | None = None,
    source_columns: set[str] | None = None,
) -> int:
    selected_columns = [
        column
        for column in model.__table__.columns
        if source_columns is None or column.name in source_columns
    ]
    offset = 0
    migrated = 0
    while True:
        rows = await source.execute(
            select(*selected_columns).select_from(model.__table__).offset(offset).limit(batch_size)
        )
        items = rows.mappings().all()
        if not items:
            break
        migrated += len(items)
        if not dry_run:
            for item in items:
                await target.merge(_clone_model(item, model, raw_store=raw_store))
            await target.commit()
        offset += batch_size
    return migrated


async def _table_names(engine) -> set[str]:
    async with engine.begin() as conn:
        return await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))


async def _column_names(engine, table_name: str) -> set[str]:
    async with engine.begin() as conn:
        return await conn.run_sync(
            lambda sync_conn: {column["name"] for column in inspect(sync_conn).get_columns(table_name)}
        )


def _clone_model(item, model, *, raw_store: LocalRawPayloadStore | None = None):
    values = dict(item)
    if raw_store and model is SignalSnapshot:
        _externalize_signal_raw_payload(values, raw_store)
    return model(**values)


def _externalize_signal_raw_payload(values: dict[str, Any], raw_store: LocalRawPayloadStore) -> None:
    payload = values.get("raw_payload")
    if not payload or values.get("raw_payload_uri"):
        return
    ref = raw_store.write_json(
        payload=payload,
        symbol=values["symbol"],
        timeframe=values["timeframe"],
        indicator=values["indicator"],
        snapshot_id=values["id"],
        collected_at=values["collected_at"],
    )
    values["raw_payload"] = {}
    values["raw_payload_uri"] = ref.uri
    values["raw_payload_sha256"] = ref.sha256
    values["raw_payload_bytes"] = ref.bytes
    values["raw_payload_compression"] = ref.compression


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
