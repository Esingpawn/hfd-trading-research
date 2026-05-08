from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SqliteIndexSpec:
    name: str
    table: str
    columns: str
    reason: str

    @property
    def create_sql(self) -> str:
        return f"CREATE INDEX IF NOT EXISTS {self.name} ON {self.table} ({self.columns})"


SQLITE_INDEX_SPECS: tuple[SqliteIndexSpec, ...] = (
    SqliteIndexSpec(
        "idx_signal_snapshots_lookup_latest",
        "signal_snapshots",
        "symbol, timeframe, indicator, created_at DESC",
        "latest snapshot lookup for dashboard scoring and completeness",
    ),
    SqliteIndexSpec(
        "idx_signal_snapshots_indicator_series",
        "signal_snapshots",
        "indicator, symbol, timeframe, created_at DESC",
        "experiment coverage and indicator-series scans",
    ),
    SqliteIndexSpec(
        "idx_price_snapshots_symbol_collected",
        "price_snapshots",
        "symbol, collected_at",
        "future-return labeling and latest-price lookups",
    ),
    SqliteIndexSpec(
        "idx_collection_runs_started",
        "collection_runs",
        "started_at DESC",
        "latest collection run cache invalidation and runtime status",
    ),
    SqliteIndexSpec(
        "idx_strategy_decisions_symbol_created",
        "strategy_decisions",
        "symbol, created_at DESC",
        "symbol-level decision history",
    ),
    SqliteIndexSpec(
        "idx_signal_observations_name_status_observed",
        "signal_observations",
        "signal_name, status, observed_at",
        "signal effectiveness and attribution backfill",
    ),
    SqliteIndexSpec(
        "idx_signal_observations_role_status_observed",
        "signal_observations",
        "signal_role, status, observed_at",
        "role-level effectiveness summaries",
    ),
    SqliteIndexSpec(
        "idx_paper_trades_status_opened",
        "paper_trades",
        "status, opened_at DESC",
        "open-trade marking and recent paper trade display",
    ),
)
