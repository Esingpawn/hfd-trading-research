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
        "ix_signal_snapshots_collection_run_id",
        "signal_snapshots",
        "collection_run_id",
        "research sample coverage by collection run",
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
        "ix_feature_events_indicator_feature_ts",
        "feature_events",
        "indicator, feature_name, event_ts",
        "feature effectiveness grouping and event labeling",
    ),
    SqliteIndexSpec(
        "ix_feature_events_indicator_ts_id",
        "feature_events",
        "indicator, event_ts, id",
        "darkflow playbook sampling by official indicator",
    ),
    SqliteIndexSpec(
        "ix_feature_labels_horizon_status",
        "feature_labels",
        "horizon, status",
        "feature label backfill and effectiveness scans",
    ),
    SqliteIndexSpec(
        "ix_feature_labels_horizon_status_event",
        "feature_labels",
        "horizon, status, feature_event_id",
        "materialized research report joins",
    ),
    SqliteIndexSpec(
        "ix_feature_events_event_ts_id",
        "feature_events",
        "event_ts, id",
        "balanced feature report sampling by event day",
    ),
    SqliteIndexSpec(
        "ix_experiment_runs_name_status_created",
        "experiment_runs",
        "name, status, created_at DESC",
        "latest materialized research report lookup",
    ),
    SqliteIndexSpec(
        "idx_paper_trades_status_opened",
        "paper_trades",
        "status, opened_at DESC",
        "open-trade marking and recent paper trade display",
    ),
    SqliteIndexSpec(
        "ix_shadow_paper_trades_strategy_status",
        "shadow_paper_trades",
        "strategy_name, status",
        "shadow paper performance by candidate strategy",
    ),
    SqliteIndexSpec(
        "ix_shadow_paper_trades_candidate",
        "shadow_paper_trades",
        "candidate_type, candidate_key",
        "shadow paper candidate lookup",
    ),
    SqliteIndexSpec(
        "ix_darkflow_zones_symbol_timeframe_detected",
        "darkflow_zones",
        "symbol, timeframe, detected_at",
        "darkflow zone backfill and latest interaction lookup",
    ),
    SqliteIndexSpec(
        "ix_darkflow_zones_indicator_detected",
        "darkflow_zones",
        "indicator, detected_at",
        "darkflow zone sampling by official indicator",
    ),
    SqliteIndexSpec(
        "ix_darkflow_interactions_playbook_event",
        "darkflow_interactions",
        "playbook, event_ts",
        "latest darkflow interaction backtest lookup",
    ),
    SqliteIndexSpec(
        "ix_darkflow_interactions_symbol_timeframe_event",
        "darkflow_interactions",
        "symbol, timeframe, event_ts",
        "darkflow interaction grouping and shadow replay",
    ),
)
