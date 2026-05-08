from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db_indexes import SQLITE_INDEX_SPECS


SQLITE_TABLES = (
    "signal_snapshots",
    "price_snapshots",
    "signal_observations",
    "strategy_decisions",
    "paper_trades",
    "collection_runs",
    "backtest_runs",
)


async def storage_health(session: AsyncSession) -> dict[str, Any]:
    settings = get_settings()
    db_path = _sqlite_path(settings.database_url)
    table_counts = await _table_counts(session)
    page_stats = await _sqlite_page_stats(session) if db_path else {}
    return {
        "database_url_kind": _database_kind(settings.database_url),
        "sqlite": _sqlite_files_payload(db_path),
        "page_stats": page_stats,
        "tables": table_counts,
        "indexes": await _index_payload(session),
        "raw_payload": await _raw_payload_estimate(session),
        "recommendations": _recommendations(db_path, table_counts, page_stats),
    }


async def ensure_performance_indexes(session: AsyncSession) -> dict[str, Any]:
    created_or_existing = []
    for spec in SQLITE_INDEX_SPECS:
        await session.execute(text(spec.create_sql))
        created_or_existing.append(
            {
                "name": spec.name,
                "table": spec.table,
                "columns": spec.columns,
                "reason": spec.reason,
            }
        )
    await session.commit()
    return {"status": "ok", "indexes": created_or_existing}


async def sqlite_checkpoint(session: AsyncSession, *, truncate: bool = True) -> dict[str, Any]:
    mode = "TRUNCATE" if truncate else "PASSIVE"
    rows = await session.execute(text(f"PRAGMA wal_checkpoint({mode})"))
    values = list(rows.first() or ())
    await session.commit()
    return {
        "status": "ok",
        "mode": mode,
        "busy": values[0] if len(values) > 0 else None,
        "log_frames": values[1] if len(values) > 1 else None,
        "checkpointed_frames": values[2] if len(values) > 2 else None,
    }


async def sqlite_optimize(session: AsyncSession) -> dict[str, Any]:
    await session.execute(text("PRAGMA optimize"))
    await session.commit()
    return {"status": "ok", "operation": "PRAGMA optimize"}


def _sqlite_path(database_url: str) -> Path | None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return None
    raw_path = database_url.removeprefix(prefix)
    if raw_path == ":memory:":
        return None
    return Path(raw_path)


def _database_kind(database_url: str) -> str:
    if database_url.startswith("sqlite"):
        return "sqlite"
    if database_url.startswith("postgres"):
        return "postgresql"
    return "other"


def _sqlite_files_payload(db_path: Path | None) -> dict[str, Any]:
    if db_path is None:
        return {"path": None, "files": [], "total_bytes": 0}
    files = []
    total = 0
    for path in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        size = path.stat().st_size if path.exists() else 0
        total += size
        files.append(
            {
                "path": str(path),
                "exists": path.exists(),
                "bytes": size,
                "mb": round(size / 1024 / 1024, 2),
            }
        )
    return {"path": str(db_path), "files": files, "total_bytes": total, "total_gb": round(total / 1024**3, 3)}


async def _table_counts(session: AsyncSession) -> list[dict[str, Any]]:
    rows = []
    for table in SQLITE_TABLES:
        count = await session.scalar(text(f"SELECT COUNT(*) FROM {table}"))
        rows.append({"table": table, "rows": int(count or 0)})
    return rows


async def _sqlite_page_stats(session: AsyncSession) -> dict[str, Any]:
    page_count = int((await session.execute(text("PRAGMA page_count"))).scalar_one())
    page_size = int((await session.execute(text("PRAGMA page_size"))).scalar_one())
    freelist_count = int((await session.execute(text("PRAGMA freelist_count"))).scalar_one())
    journal_mode = str((await session.execute(text("PRAGMA journal_mode"))).scalar_one())
    return {
        "page_count": page_count,
        "page_size": page_size,
        "freelist_count": freelist_count,
        "db_bytes": page_count * page_size,
        "free_bytes": freelist_count * page_size,
        "journal_mode": journal_mode,
    }


async def _index_payload(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(
        text("SELECT name, tbl_name FROM sqlite_master WHERE type='index'")
    )
    existing = {str(row[0]) for row in rows.all()}
    required = [
        {
            "name": spec.name,
            "table": spec.table,
            "columns": spec.columns,
            "reason": spec.reason,
            "present": spec.name in existing,
        }
        for spec in SQLITE_INDEX_SPECS
    ]
    return {
        "required": required,
        "missing_required": [item["name"] for item in required if not item["present"]],
        "total_indexes": len(existing),
    }


async def _raw_payload_estimate(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(
        text(
            """
            SELECT
              COUNT(*) AS rows,
              COALESCE(SUM(LENGTH(raw_payload)), 0) AS raw_bytes,
              COALESCE(SUM(LENGTH(summary_payload)), 0) AS summary_bytes,
              COALESCE(AVG(LENGTH(raw_payload)), 0) AS avg_raw_bytes
            FROM signal_snapshots
            """
        )
    )
    row = rows.first()
    raw_bytes = int(row.raw_bytes or 0) if row else 0
    summary_bytes = int(row.summary_bytes or 0) if row else 0
    return {
        "rows": int(row.rows or 0) if row else 0,
        "raw_bytes": raw_bytes,
        "raw_gb": round(raw_bytes / 1024**3, 3),
        "summary_bytes": summary_bytes,
        "summary_mb": round(summary_bytes / 1024**2, 2),
        "avg_raw_kb": round(float(row.avg_raw_bytes or 0) / 1024, 2) if row else 0,
    }


def _recommendations(
    db_path: Path | None,
    table_counts: list[dict[str, Any]],
    page_stats: dict[str, Any],
) -> list[str]:
    rows_by_table = {row["table"]: row["rows"] for row in table_counts}
    recommendations = []
    db_size = db_path.stat().st_size if db_path and db_path.exists() else 0
    if db_size > 2 * 1024**3:
        recommendations.append("Move raw HFD payloads out of SQLite before server deployment.")
    if rows_by_table.get("signal_snapshots", 0) > 10_000:
        recommendations.append("Persist latest-state/stat tables so dashboard queries avoid raw snapshot scans.")
    if int(page_stats.get("free_bytes") or 0) > 256 * 1024**2:
        recommendations.append("Run VACUUM during a maintenance window to reclaim free pages.")
    recommendations.append("Run checkpoint/optimize after large collection batches.")
    return recommendations
