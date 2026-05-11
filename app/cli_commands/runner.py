from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Sequence

from app.constants import CORE_INDICATORS, REQUIRED_SCORING_INDICATORS, RESEARCH_INDICATORS, TIMEFRAMES
from app.application.storage import get_storage_health, run_storage_maintenance
from app.application.task_worker import run_task_worker
from app.cli_commands.db_helpers import collect_once, collection_result_payload, latest_collection_run
from app.cli_commands.utils import jsonable
from app.db import SessionLocal, engine, init_db
from app.hfd.client import HfdClient
from app.services.backtest_batch import run_backtest_batch
from app.services.backtest import run_cost_band_retest_backtest
from app.services.collection_schedule import research_due_timeframes, research_intervals
from app.services.collector import SnapshotCollector
from app.services.feature_candidates import (
    feature_candidate_screen,
    feature_paper_ab,
    feature_segment_candidate_screen,
    feature_segment_paper_ab,
)
from app.services.experiment_loop import run_experiment_backfill
from app.services.features import (
    backfill_feature_events,
    backfill_feature_labels,
    feature_effectiveness,
    reset_feature_research,
    refresh_feature_research,
)
from app.services.paper import mark_open_trades, paper_scan
from app.services.paper_loop import paper_loop_decision
from app.services.signal_attribution import backfill_signal_outcomes, signal_effectiveness
from app.services.strategy import evaluate_symbol
from app.services.telegram import TelegramClient, extract_chat_candidates


RUNTIME_DIR = Path("data/runtime")
LOG_DIR = Path("data/logs")
RUNTIME_HEARTBEAT_SECONDS = 30


def _runtime_metadata(
    name: str,
    *,
    command: str,
    interval_seconds: int | float | None = None,
    heartbeat_ttl_seconds: int | float | None = None,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "pid": os.getpid(),
        "started_at": _utc_now_iso(),
        "stdout_log": str(LOG_DIR / f"{name}.out.log"),
        "stderr_log": str(LOG_DIR / f"{name}.err.log"),
        "command": command,
        "containerized": _running_in_container(),
    }
    if interval_seconds is not None:
        payload["interval_seconds"] = interval_seconds
    if heartbeat_ttl_seconds is not None:
        payload["heartbeat_ttl_seconds"] = heartbeat_ttl_seconds
    payload.update(extra)
    return payload


def _touch_runtime(name: str, metadata: dict[str, object], **updates: object) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        payload = dict(metadata)
        payload.update(updates)
        payload["pid"] = os.getpid()
        payload["heartbeat_at"] = _utc_now_iso()
        metadata.update(updates)
        metadata["pid"] = payload["pid"]
        metadata["heartbeat_at"] = payload["heartbeat_at"]
        path = RUNTIME_DIR / f"{name}.json"
        tmp_path = path.with_name(f"{path.name}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
        (RUNTIME_DIR / f"{name}.pid").write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return


def _start_runtime_heartbeat(
    name: str,
    metadata: dict[str, object],
) -> asyncio.Task[None]:
    _touch_runtime(name, metadata, status="running")
    return asyncio.create_task(_runtime_heartbeat_loop(name, metadata))


async def _stop_runtime_heartbeat(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _runtime_heartbeat_loop(name: str, metadata: dict[str, object]) -> None:
    while True:
        _touch_runtime(name, metadata, status="running")
        await asyncio.sleep(RUNTIME_HEARTBEAT_SECONDS)


def _heartbeat_ttl(interval_seconds: int | float) -> int:
    return max(int(float(interval_seconds) * 2) + 60, 120)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HFD research system CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create database tables")

    collect = subparsers.add_parser("collect", help="Collect HFD signal snapshots")
    collect.add_argument("--coins", nargs="*", help="Coin symbols, e.g. BTC ETH")
    collect.add_argument(
        "--timeframes",
        nargs="*",
        choices=["short", "mid", "long"],
        help="Timeframe names",
    )
    collect.add_argument(
        "--indicators",
        nargs="*",
        choices=list(CORE_INDICATORS),
        help="Indicator names",
    )
    collect.add_argument("--dry-run", action="store_true", help="Fetch but do not write")

    collect_core = subparsers.add_parser("collect-scoring-core", help="Collect indicators required by scoring")
    collect_core.add_argument("--coins", nargs="*", help="Coin symbols, e.g. BTC ETH")
    collect_core.add_argument(
        "--timeframes",
        nargs="*",
        choices=["short", "mid", "long"],
        help="Timeframe names",
    )
    collect_core.add_argument("--dry-run", action="store_true", help="Fetch but do not write")

    loop = subparsers.add_parser("collect-loop", help="Run collection repeatedly")
    loop.add_argument("--coins", nargs="*", help="Coin symbols, e.g. BTC ETH")
    loop.add_argument(
        "--timeframes",
        nargs="*",
        choices=["short", "mid", "long"],
        help="Timeframe names",
    )
    loop.add_argument(
        "--indicators",
        nargs="*",
        choices=list(CORE_INDICATORS),
        help="Indicator names",
    )
    loop.add_argument("--dry-run", action="store_true", help="Fetch but do not write")
    loop.add_argument(
        "--interval-seconds",
        type=int,
        default=1800,
        help="Seconds between collection runs",
    )
    loop.add_argument(
        "--max-runs",
        type=int,
        default=0,
        help="Stop after N runs. 0 means run until interrupted",
    )

    tiered_loop = subparsers.add_parser(
        "collect-tiered-loop",
        help="Run high-frequency scoring collection and lower-frequency research collection",
    )
    tiered_loop.add_argument("--coins", nargs="*", help="Coin symbols, e.g. BTC ETH")
    tiered_loop.add_argument(
        "--timeframes",
        nargs="*",
        choices=["short", "mid", "long"],
        help="Timeframe names",
    )
    tiered_loop.add_argument("--dry-run", action="store_true", help="Fetch but do not write")
    tiered_loop.add_argument(
        "--core-interval-seconds",
        type=int,
        default=1800,
        help="Seconds between scoring core collection runs",
    )
    tiered_loop.add_argument(
        "--research-short-interval-seconds",
        type=int,
        default=1800,
        help="Seconds between short timeframe research refreshes",
    )
    tiered_loop.add_argument(
        "--research-mid-interval-seconds",
        type=int,
        default=3600,
        help="Seconds between mid timeframe research refreshes",
    )
    tiered_loop.add_argument(
        "--research-long-interval-seconds",
        type=int,
        default=14400,
        help="Seconds between long timeframe research refreshes",
    )
    tiered_loop.add_argument(
        "--max-runs",
        type=int,
        default=0,
        help="Stop after N core runs. 0 means run until interrupted",
    )

    backtest = subparsers.add_parser("backtest", help="Run static historical backtest")
    backtest.add_argument("--coin", required=True, help="Coin symbol, e.g. BTC")
    backtest.add_argument(
        "--timeframe",
        default="short",
        choices=["short", "mid", "long"],
        help="Timeframe name",
    )
    backtest.add_argument("--stop-pct", type=float, default=0.01)
    backtest.add_argument("--target-pct", type=float, default=0.02)
    backtest.add_argument("--max-hold-bars", type=int, default=24)
    backtest.add_argument("--limit-zones", type=int, default=100)
    backtest.add_argument("--show-trades", type=int, default=5)

    batch = subparsers.add_parser("backtest-batch", help="Run batch historical screening")
    batch.add_argument("--coins", nargs="*", help="Coin symbols")
    batch.add_argument(
        "--timeframes",
        nargs="*",
        choices=["short", "mid", "long"],
        help="Timeframe names",
    )
    batch.add_argument("--stop-pct", type=float, default=0.01)
    batch.add_argument("--target-pct", type=float, default=0.02)
    batch.add_argument("--max-hold-bars", type=int, default=24)
    batch.add_argument("--limit-zones", type=int, default=100)
    batch.add_argument("--top", type=int, default=20)
    batch.add_argument("--no-persist", action="store_true")

    scan = subparsers.add_parser("paper-scan", help="Evaluate latest snapshots and open paper trades")
    scan.add_argument("--coins", nargs="*", help="Coin symbols")
    scan.add_argument("--dry-run", action="store_true")
    scan.add_argument("--notify", action="store_true", help="Send Telegram notifications for opened trades")

    subparsers.add_parser("paper-mark", help="Mark open paper trades against latest prices")

    signal_backfill = subparsers.add_parser("signals-backfill", help="Backfill signal attribution outcome labels")
    signal_backfill.add_argument("--limit", type=int, default=500)

    signal_report = subparsers.add_parser("signals-report", help="Show signal effectiveness rankings")
    signal_report.add_argument("--min-samples", type=int, default=1)
    signal_report.add_argument("--horizon", choices=["30m", "1h", "4h", "24h"], default="4h")

    feature_backfill = subparsers.add_parser("features-backfill", help="Backfill standardized feature events")
    feature_backfill.add_argument("--limit", type=int, default=500)
    feature_backfill.add_argument("--indicators", nargs="*", help="Indicator keys to scan")

    feature_labels = subparsers.add_parser("features-label", help="Backfill future-return labels for feature events")
    feature_labels.add_argument("--limit", type=int, default=1000)
    feature_labels.add_argument("--horizons", nargs="*", choices=["30m", "1h", "4h", "24h"])
    feature_labels.add_argument("--refresh-labeled", action="store_true", help="Recompute labels that are already labeled")

    feature_reset = subparsers.add_parser("features-reset", help="Delete feature events and labels before rebuilding research data")
    feature_reset.add_argument("--indicators", nargs="*", help="Indicator keys to reset")

    feature_refresh = subparsers.add_parser("features-refresh", help="Backfill feature events, labels, and print effectiveness")
    feature_refresh.add_argument("--limit", type=int, default=500)
    feature_refresh.add_argument("--indicators", nargs="*", help="Indicator keys to scan")
    feature_refresh.add_argument("--horizons", nargs="*", choices=["30m", "1h", "4h", "24h"])
    feature_refresh.add_argument("--min-samples", type=int, default=5)

    feature_report = subparsers.add_parser("features-report", help="Show feature event effectiveness rankings")
    feature_report.add_argument("--min-samples", type=int, default=5)
    feature_report.add_argument("--horizon", choices=["30m", "1h", "4h", "24h"], default="4h")
    feature_report.add_argument("--limit", type=int, default=10000)

    feature_candidates = subparsers.add_parser("features-candidates", help="Screen labeled feature events for research candidates")
    feature_candidates.add_argument("--horizon", choices=["30m", "1h", "4h", "24h"], default="30m")
    feature_candidates.add_argument("--min-samples", type=int, default=30)
    feature_candidates.add_argument("--min-win-rate", type=float, default=0.52)
    feature_candidates.add_argument("--min-profit-factor", type=float, default=1.2)
    feature_candidates.add_argument("--min-avg-return", type=float, default=0.0)
    feature_candidates.add_argument("--segment-min-samples", type=int, default=5)
    feature_candidates.add_argument("--min-segments", type=int, default=2)
    feature_candidates.add_argument("--limit", type=int, default=20000)
    feature_candidates.add_argument("--persist", action="store_true", help="Save the report as an experiment run")

    feature_ab = subparsers.add_parser("features-paper-ab", help="Run report-only feature paper A/B from labels")
    feature_ab.add_argument("--horizon", choices=["30m", "1h", "4h", "24h"], default="30m")
    feature_ab.add_argument("--min-samples", type=int, default=30)
    feature_ab.add_argument("--min-win-rate", type=float, default=0.52)
    feature_ab.add_argument("--min-profit-factor", type=float, default=1.2)
    feature_ab.add_argument("--min-avg-return", type=float, default=0.0)
    feature_ab.add_argument("--segment-min-samples", type=int, default=5)
    feature_ab.add_argument("--min-segments", type=int, default=2)
    feature_ab.add_argument("--candidate-limit", type=int, default=20)
    feature_ab.add_argument("--limit", type=int, default=20000)
    feature_ab.add_argument("--persist", action="store_true", help="Save the report as an experiment run")

    segment_candidates = subparsers.add_parser("features-segment-candidates", help="Screen feature candidates by symbol/timeframe segment")
    segment_candidates.add_argument("--horizon", choices=["30m", "1h", "4h", "24h"], default="30m")
    segment_candidates.add_argument("--min-samples", type=int, default=30)
    segment_candidates.add_argument("--min-win-rate", type=float, default=0.52)
    segment_candidates.add_argument("--min-profit-factor", type=float, default=1.2)
    segment_candidates.add_argument("--min-avg-return", type=float, default=0.0)
    segment_candidates.add_argument("--no-dedupe-research-samples", action="store_true", help="Disable research sample time-bucket dedupe")
    segment_candidates.add_argument("--dedupe-bucket-minutes", type=int, default=30)
    segment_candidates.add_argument("--min-unique-time-buckets", type=int, default=3)
    segment_candidates.add_argument("--min-unique-event-days", type=int, default=2)
    segment_candidates.add_argument("--min-unique-market-windows", type=int, default=2)
    segment_candidates.add_argument("--min-unique-collection-runs", type=int, default=2)
    segment_candidates.add_argument("--market-window-hours", type=int, default=8)
    segment_candidates.add_argument("--max-same-return-samples", type=int, default=10)
    segment_candidates.add_argument("--max-return-cluster-ratio", type=float, default=0.75)
    segment_candidates.add_argument("--limit", type=int, default=20000)
    segment_candidates.add_argument("--persist", action="store_true", help="Save the report as an experiment run")

    segment_ab = subparsers.add_parser("features-segment-paper-ab", help="Run report-only segment feature paper A/B from labels")
    segment_ab.add_argument("--horizon", choices=["30m", "1h", "4h", "24h"], default="30m")
    segment_ab.add_argument("--min-samples", type=int, default=30)
    segment_ab.add_argument("--min-win-rate", type=float, default=0.52)
    segment_ab.add_argument("--min-profit-factor", type=float, default=1.2)
    segment_ab.add_argument("--min-avg-return", type=float, default=0.0)
    segment_ab.add_argument("--no-dedupe-research-samples", action="store_true", help="Disable research sample time-bucket dedupe")
    segment_ab.add_argument("--dedupe-bucket-minutes", type=int, default=30)
    segment_ab.add_argument("--min-unique-time-buckets", type=int, default=3)
    segment_ab.add_argument("--min-unique-event-days", type=int, default=2)
    segment_ab.add_argument("--min-unique-market-windows", type=int, default=2)
    segment_ab.add_argument("--min-unique-collection-runs", type=int, default=2)
    segment_ab.add_argument("--market-window-hours", type=int, default=8)
    segment_ab.add_argument("--max-same-return-samples", type=int, default=10)
    segment_ab.add_argument("--max-return-cluster-ratio", type=float, default=0.75)
    segment_ab.add_argument("--candidate-limit", type=int, default=50)
    segment_ab.add_argument("--limit", type=int, default=20000)
    segment_ab.add_argument("--persist", action="store_true", help="Save the report as an experiment run")

    subparsers.add_parser("storage-health", help="Show database storage health")
    storage_maintain = subparsers.add_parser("storage-maintain", help="Run safe database maintenance")
    storage_maintain.add_argument("--indexes", action="store_true", help="Ensure required performance indexes")
    storage_maintain.add_argument("--checkpoint", action="store_true", help="Run SQLite WAL checkpoint")
    storage_maintain.add_argument("--passive-checkpoint", action="store_true", help="Use PASSIVE instead of TRUNCATE checkpoint")
    storage_maintain.add_argument("--optimize", action="store_true", help="Run SQLite optimize or PostgreSQL ANALYZE")

    paper_loop = subparsers.add_parser("paper-loop", help="Run paper mark/scan after new collection runs")
    paper_loop.add_argument("--coins", nargs="*", help="Coin symbols")
    paper_loop.add_argument("--notify", action="store_true", help="Send Telegram notifications")
    paper_loop.add_argument(
        "--interval-seconds",
        type=int,
        default=60,
        help="Seconds between checks for new collection runs",
    )
    paper_loop.add_argument(
        "--max-runs",
        type=int,
        default=0,
        help="Stop after N processed collection runs. 0 means run until interrupted",
    )
    paper_loop.add_argument(
        "--process-existing",
        action="store_true",
        help="Process the latest existing collection immediately instead of waiting for the next one",
    )

    experiment_loop = subparsers.add_parser(
        "experiment-loop",
        help="Run signal and feature research backfills on a schedule",
    )
    experiment_loop.add_argument("--limit", type=int, default=500)
    experiment_loop.add_argument(
        "--feature-limit",
        type=int,
        default=500,
        help="Latest signal snapshots to scan for research feature events each run",
    )
    experiment_loop.add_argument(
        "--feature-label-limit",
        type=int,
        default=5000,
        help="Latest feature events to label each run",
    )
    experiment_loop.add_argument(
        "--feature-horizons",
        nargs="*",
        choices=["30m", "1h", "4h", "24h"],
        default=["30m"],
        help="Feature label horizons to maintain for candidate research",
    )
    experiment_loop.add_argument(
        "--no-feature-research",
        action="store_true",
        help="Only backfill signal attribution outcomes",
    )
    experiment_loop.add_argument(
        "--interval-seconds",
        type=int,
        default=300,
        help="Seconds between experiment backfill runs",
    )
    experiment_loop.add_argument(
        "--max-runs",
        type=int,
        default=0,
        help="Stop after N runs. 0 means run until interrupted",
    )

    task_worker = subparsers.add_parser("task-worker", help="Consume queued Redis task_runs")
    task_worker.add_argument(
        "--max-tasks",
        type=int,
        default=0,
        help="Stop after N processed or failed tasks. 0 means run until interrupted",
    )
    task_worker.add_argument(
        "--idle-sleep-seconds",
        type=float,
        default=1.0,
        help="Sleep duration after an empty dequeue",
    )
    task_worker.add_argument(
        "--dequeue-timeout-seconds",
        type=int,
        default=5,
        help="Redis BLPOP timeout in seconds",
    )

    eval_cmd = subparsers.add_parser("evaluate", help="Evaluate latest strategy score")
    eval_cmd.add_argument("--coin", required=True)
    eval_cmd.add_argument("--dry-run", action="store_true")

    tg = subparsers.add_parser("telegram", help="Telegram bot utilities")
    tg_sub = tg.add_subparsers(dest="telegram_command", required=True)
    tg_sub.add_parser("status", help="Check bot status")
    tg_sub.add_parser("updates", help="Show recent chat candidates")
    tg_send = tg_sub.add_parser("send", help="Send a Telegram message")
    tg_send.add_argument("--text", required=True)
    tg_send.add_argument("--chat-id")
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init-db":
        try:
            await init_db()
            print("database initialized")
            return 0
        finally:
            await engine.dispose()

    if args.command == "collect":
        async with SessionLocal() as session:
            collector = SnapshotCollector(session)
            try:
                result = await collector.collect(
                    assets=args.coins,
                    timeframes=args.timeframes,
                    indicators=args.indicators,
                    dry_run=args.dry_run,
                )
            finally:
                await collector.close()
        print(
            {
                "status": result.status,
                "dry_run": result.dry_run,
                "assets": result.assets,
                "timeframes": result.timeframes,
                "indicators": result.indicators,
                "snapshots_written": result.snapshots_written,
                "prices_written": result.prices_written,
                "errors": result.errors,
            }
        )
        await engine.dispose()
        return 0 if not result.errors else 2

    if args.command == "collect-scoring-core":
        async with SessionLocal() as session:
            collector = SnapshotCollector(session)
            try:
                result = await collector.collect(
                    assets=args.coins,
                    timeframes=args.timeframes,
                    indicators=list(REQUIRED_SCORING_INDICATORS),
                    dry_run=args.dry_run,
                )
            finally:
                await collector.close()
        print(
            {
                "status": result.status,
                "dry_run": result.dry_run,
                "assets": result.assets,
                "timeframes": result.timeframes,
                "indicators": result.indicators,
                "snapshots_written": result.snapshots_written,
                "prices_written": result.prices_written,
                "errors": result.errors,
            }
        )
        await engine.dispose()
        return 0 if not result.errors else 2

    if args.command == "collect-loop":
        runtime_meta = _runtime_metadata(
            "collect-core-loop",
            command="python -m app.cli collect-loop",
            interval_seconds=args.interval_seconds,
            heartbeat_ttl_seconds=_heartbeat_ttl(args.interval_seconds),
            coins=args.coins,
            timeframes=args.timeframes,
            indicators=args.indicators,
            dry_run=args.dry_run,
        )
        heartbeat_task = _start_runtime_heartbeat("collect-core-loop", runtime_meta)
        run_number = 0
        try:
            while True:
                run_number += 1
                _touch_runtime("collect-core-loop", runtime_meta, run_number=run_number)
                async with SessionLocal() as session:
                    collector = SnapshotCollector(session)
                    try:
                        result = await collector.collect(
                            assets=args.coins,
                            timeframes=args.timeframes,
                            indicators=args.indicators,
                            dry_run=args.dry_run,
                        )
                    finally:
                        await collector.close()
                print(
                    {
                        "run": run_number,
                        "status": result.status,
                        "snapshots_written": result.snapshots_written,
                        "prices_written": result.prices_written,
                        "errors": result.errors,
                    }
                )
                if args.max_runs and run_number >= args.max_runs:
                    break
                await asyncio.sleep(args.interval_seconds)
        except KeyboardInterrupt:
            print("collection loop interrupted")
        finally:
            await _stop_runtime_heartbeat(heartbeat_task)
            await engine.dispose()
        return 0

    if args.command == "collect-tiered-loop":
        selected_timeframes = args.timeframes or list(TIMEFRAMES.keys())
        intervals = research_intervals(
            short=args.research_short_interval_seconds,
            mid=args.research_mid_interval_seconds,
            long=args.research_long_interval_seconds,
        )
        runtime_meta = _runtime_metadata(
            "collect-core-loop",
            command="python -m app.cli collect-tiered-loop",
            interval_seconds=args.core_interval_seconds,
            heartbeat_ttl_seconds=_heartbeat_ttl(args.core_interval_seconds),
            coins=args.coins,
            timeframes=selected_timeframes,
            indicators=list(REQUIRED_SCORING_INDICATORS),
            mode="tiered",
            research_indicators=list(RESEARCH_INDICATORS),
            research_intervals=intervals,
            dry_run=args.dry_run,
        )
        heartbeat_task = _start_runtime_heartbeat("collect-core-loop", runtime_meta)
        last_research_completed_at: dict[str, float] = {}
        run_number = 0
        try:
            while True:
                run_number += 1
                _touch_runtime("collect-core-loop", runtime_meta, run_number=run_number)
                core_result = await collect_once(
                    assets=args.coins,
                    timeframes=selected_timeframes,
                    indicators=list(REQUIRED_SCORING_INDICATORS),
                    dry_run=args.dry_run,
                )
                payload: dict[str, object] = {
                    "run": run_number,
                    "mode": "tiered",
                    "core": collection_result_payload(core_result),
                    "research": [],
                }

                now = time.monotonic()
                due_timeframes = research_due_timeframes(
                    selected_timeframes,
                    last_research_completed_at,
                    now,
                    intervals,
                )
                for timeframe in due_timeframes:
                    research_result = await collect_once(
                        assets=args.coins,
                        timeframes=[timeframe],
                        indicators=list(RESEARCH_INDICATORS),
                        dry_run=args.dry_run,
                    )
                    payload["research"].append(
                        {"timeframe": timeframe, **collection_result_payload(research_result)}
                    )
                    if not research_result.errors:
                        last_research_completed_at[timeframe] = time.monotonic()

                print(json.dumps(jsonable(payload), ensure_ascii=False), flush=True)
                if args.max_runs and run_number >= args.max_runs:
                    break
                await asyncio.sleep(args.core_interval_seconds)
        except KeyboardInterrupt:
            print("tiered collection loop interrupted")
        finally:
            await _stop_runtime_heartbeat(heartbeat_task)
            await engine.dispose()
        return 0

    if args.command == "backtest":
        coin = args.coin.upper()
        interval = TIMEFRAMES[args.timeframe].interval
        async with HfdClient() as client:
            payload = await client.fetch_pro_data(coin, interval, "smart_money_cost")
        result = run_cost_band_retest_backtest(
            payload=payload,
            symbol=f"{coin}USDT",
            interval=interval,
            stop_pct=args.stop_pct,
            target_pct=args.target_pct,
            max_hold_bars=args.max_hold_bars,
            limit_zones=args.limit_zones,
        )
        print(
            json.dumps(
                {
                    "summary": {
                        "strategy": result.summary.strategy,
                        "symbol": result.summary.symbol,
                        "interval": result.summary.interval,
                        "trade_count": result.summary.trade_count,
                        "win_rate": round(result.summary.win_rate, 4),
                        "avg_pnl_pct": round(result.summary.avg_pnl_pct, 6),
                        "total_pnl_pct": round(result.summary.total_pnl_pct, 6),
                        "profit_factor": (
                            round(result.summary.profit_factor, 4)
                            if result.summary.profit_factor is not None
                            else None
                        ),
                        "max_drawdown_pct": round(result.summary.max_drawdown_pct, 6),
                        "notes": result.summary.notes,
                    },
                    "trades": [
                        {
                            "direction": trade.direction,
                            "entry_ts": trade.entry_ts,
                            "entry_price": trade.entry_price,
                            "exit_ts": trade.exit_ts,
                            "exit_price": trade.exit_price,
                            "exit_reason": trade.exit_reason,
                            "pnl_pct": round(trade.pnl_pct, 6),
                            "r_multiple": round(trade.r_multiple, 4),
                        }
                        for trade in result.trades[: args.show_trades]
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        await engine.dispose()
        return 0

    if args.command == "backtest-batch":
        async with SessionLocal() as session:
            result = await run_backtest_batch(
                session=session,
                coins=args.coins,
                timeframes=args.timeframes,
                stop_pct=args.stop_pct,
                target_pct=args.target_pct,
                max_hold_bars=args.max_hold_bars,
                limit_zones=args.limit_zones,
                persist=not args.no_persist,
            )
        print(
            json.dumps(
                {
                    "strategy": result["strategy"],
                    "status": result["status"],
                    "top": result["results"][: args.top],
                    "errors": result["errors"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        await engine.dispose()
        return 0 if not result["errors"] else 2

    if args.command == "evaluate":
        async with SessionLocal() as session:
            result = await evaluate_symbol(session, args.coin, dry_run=args.dry_run)
        print(json.dumps(jsonable(result.__dict__), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "paper-scan":
        async with SessionLocal() as session:
            coins = [c.upper() for c in args.coins] if args.coins else ["BTC", "ETH"]
            result = await paper_scan(
                session,
                coins=coins,
                dry_run=args.dry_run,
                notify=args.notify,
            )
        print(json.dumps(jsonable(result.__dict__), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "paper-mark":
        async with SessionLocal() as session:
            result = await mark_open_trades(session)
        print(json.dumps(jsonable(result), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "signals-backfill":
        async with SessionLocal() as session:
            result = await backfill_signal_outcomes(session, limit=args.limit)
        print(json.dumps(jsonable(result.__dict__), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "signals-report":
        async with SessionLocal() as session:
            result = await signal_effectiveness(
                session,
                min_samples=args.min_samples,
                horizon=args.horizon,
            )
        print(json.dumps(jsonable(result), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "features-backfill":
        async with SessionLocal() as session:
            result = await backfill_feature_events(
                session,
                limit=args.limit,
                indicators=args.indicators,
            )
        print(json.dumps(jsonable(result.__dict__), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "features-label":
        async with SessionLocal() as session:
            result = await backfill_feature_labels(
                session,
                limit=args.limit,
                horizons=args.horizons,
                refresh_labeled=args.refresh_labeled,
            )
        print(json.dumps(jsonable(result.__dict__), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "features-reset":
        async with SessionLocal() as session:
            result = await reset_feature_research(session, indicators=args.indicators)
        print(json.dumps(jsonable(result.__dict__), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "features-refresh":
        async with SessionLocal() as session:
            result = await refresh_feature_research(
                session,
                limit=args.limit,
                indicators=args.indicators,
                horizons=args.horizons,
                min_samples=args.min_samples,
            )
        print(json.dumps(jsonable(result), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "features-report":
        async with SessionLocal() as session:
            result = await feature_effectiveness(
                session,
                min_samples=args.min_samples,
                horizon=args.horizon,
                limit=args.limit,
            )
        print(json.dumps(jsonable(result), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "features-candidates":
        async with SessionLocal() as session:
            result = await feature_candidate_screen(
                session,
                horizon=args.horizon,
                min_samples=args.min_samples,
                min_win_rate=args.min_win_rate,
                min_profit_factor=args.min_profit_factor,
                min_avg_return=args.min_avg_return,
                segment_min_samples=args.segment_min_samples,
                min_segments=args.min_segments,
                limit=args.limit,
                persist=args.persist,
            )
        print(json.dumps(jsonable(result), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "features-paper-ab":
        async with SessionLocal() as session:
            result = await feature_paper_ab(
                session,
                horizon=args.horizon,
                min_samples=args.min_samples,
                min_win_rate=args.min_win_rate,
                min_profit_factor=args.min_profit_factor,
                min_avg_return=args.min_avg_return,
                segment_min_samples=args.segment_min_samples,
                min_segments=args.min_segments,
                candidate_limit=args.candidate_limit,
                limit=args.limit,
                persist=args.persist,
            )
        print(json.dumps(jsonable(result), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "features-segment-candidates":
        async with SessionLocal() as session:
            result = await feature_segment_candidate_screen(
                session,
                horizon=args.horizon,
                min_samples=args.min_samples,
                min_win_rate=args.min_win_rate,
                min_profit_factor=args.min_profit_factor,
                min_avg_return=args.min_avg_return,
                dedupe_research_samples=not args.no_dedupe_research_samples,
                dedupe_bucket_minutes=args.dedupe_bucket_minutes,
                min_unique_time_buckets=args.min_unique_time_buckets,
                min_unique_event_days=args.min_unique_event_days,
                min_unique_market_windows=args.min_unique_market_windows,
                min_unique_collection_runs=args.min_unique_collection_runs,
                market_window_hours=args.market_window_hours,
                max_same_return_samples=args.max_same_return_samples,
                max_return_cluster_ratio=args.max_return_cluster_ratio,
                limit=args.limit,
                persist=args.persist,
            )
        print(json.dumps(jsonable(result), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "features-segment-paper-ab":
        async with SessionLocal() as session:
            result = await feature_segment_paper_ab(
                session,
                horizon=args.horizon,
                min_samples=args.min_samples,
                min_win_rate=args.min_win_rate,
                min_profit_factor=args.min_profit_factor,
                min_avg_return=args.min_avg_return,
                dedupe_research_samples=not args.no_dedupe_research_samples,
                dedupe_bucket_minutes=args.dedupe_bucket_minutes,
                min_unique_time_buckets=args.min_unique_time_buckets,
                min_unique_event_days=args.min_unique_event_days,
                min_unique_market_windows=args.min_unique_market_windows,
                min_unique_collection_runs=args.min_unique_collection_runs,
                market_window_hours=args.market_window_hours,
                max_same_return_samples=args.max_same_return_samples,
                max_return_cluster_ratio=args.max_return_cluster_ratio,
                candidate_limit=args.candidate_limit,
                limit=args.limit,
                persist=args.persist,
            )
        print(json.dumps(jsonable(result), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "storage-health":
        async with SessionLocal() as session:
            result = await get_storage_health(session)
        print(json.dumps(jsonable(result), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "storage-maintain":
        async with SessionLocal() as session:
            result = await run_storage_maintenance(
                session,
                indexes=args.indexes,
                checkpoint=args.checkpoint,
                passive_checkpoint=args.passive_checkpoint,
                optimize=args.optimize,
            )
        print(json.dumps(jsonable(result), ensure_ascii=False, indent=2))
        await engine.dispose()
        return 0

    if args.command == "paper-loop":
        coins = [c.upper() for c in args.coins] if args.coins else ["BTC", "ETH"]
        runtime_meta = _runtime_metadata(
            "paper-loop",
            command="python -m app.cli paper-loop",
            interval_seconds=args.interval_seconds,
            heartbeat_ttl_seconds=_heartbeat_ttl(args.interval_seconds),
            coins=coins,
            notify=args.notify,
            process_existing=args.process_existing,
        )
        heartbeat_task = _start_runtime_heartbeat("paper-loop", runtime_meta)
        processed_runs = 0
        last_seen_run_id = None
        if not args.process_existing:
            async with SessionLocal() as session:
                latest = await latest_collection_run(session)
                last_seen_run_id = str(latest.id) if latest else None
        try:
            while True:
                _touch_runtime("paper-loop", runtime_meta, processed_runs=processed_runs)
                try:
                    async with SessionLocal() as session:
                        latest = await latest_collection_run(session)
                        decision = paper_loop_decision(latest, last_seen_run_id)
                        payload: dict[str, object] = {
                            "status": "waiting" if not decision.process else "processing",
                            "reason": decision.reason,
                            "collection_run_id": decision.run_id,
                        }
                        if decision.process:
                            marked = await mark_open_trades(session)
                            scanned = await paper_scan(
                                session,
                                coins=coins,
                                dry_run=False,
                                notify=args.notify,
                            )
                            processed_runs += 1
                            payload.update(
                                {
                                    "status": "processed",
                                    "processed_runs": processed_runs,
                                    "marked": marked,
                                    "scan": scanned.__dict__,
                                }
                            )
                        if decision.mark_seen:
                            last_seen_run_id = decision.run_id
                        print(json.dumps(jsonable(payload), ensure_ascii=False), flush=True)
                except Exception as exc:  # noqa: BLE001
                    print(
                        json.dumps(
                            {
                                "status": "error",
                                "reason": "paper_loop_iteration_failed",
                                "error": str(exc),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                if args.max_runs and processed_runs >= args.max_runs:
                    break
                await asyncio.sleep(args.interval_seconds)
        except KeyboardInterrupt:
            print("paper loop interrupted")
        finally:
            await _stop_runtime_heartbeat(heartbeat_task)
            await engine.dispose()
        return 0

    if args.command == "experiment-loop":
        runtime_meta = _runtime_metadata(
            "experiment-loop",
            command="python -m app.cli experiment-loop",
            interval_seconds=args.interval_seconds,
            heartbeat_ttl_seconds=_heartbeat_ttl(args.interval_seconds),
            limit=args.limit,
            feature_limit=args.feature_limit,
            feature_label_limit=args.feature_label_limit,
            feature_horizons=args.feature_horizons,
            feature_research_enabled=not args.no_feature_research,
        )
        heartbeat_task = _start_runtime_heartbeat("experiment-loop", runtime_meta)
        run_number = 0
        try:
            while True:
                run_number += 1
                _touch_runtime("experiment-loop", runtime_meta, run_number=run_number)
                try:
                    async with SessionLocal() as session:
                        result = await run_experiment_backfill(
                            session,
                            signal_limit=args.limit,
                            feature_limit=args.feature_limit,
                            feature_label_limit=args.feature_label_limit,
                            feature_horizons=args.feature_horizons,
                            include_feature_research=not args.no_feature_research,
                        )
                    print(
                        json.dumps(
                            {
                                "run": run_number,
                                "status": "processed",
                                "backfill": result["signals"],
                                "signals": result["signals"],
                                "features": result["features"],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(
                        json.dumps(
                            {
                                "run": run_number,
                                "status": "error",
                                "reason": "experiment_loop_iteration_failed",
                                "error": str(exc),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                if args.max_runs and run_number >= args.max_runs:
                    break
                await asyncio.sleep(args.interval_seconds)
        except KeyboardInterrupt:
            print("experiment loop interrupted")
        finally:
            await _stop_runtime_heartbeat(heartbeat_task)
            await engine.dispose()
        return 0

    if args.command == "task-worker":
        runtime_meta = _runtime_metadata(
            "task-worker",
            command="python -m app.cli task-worker",
            interval_seconds=max(args.idle_sleep_seconds, 1),
            heartbeat_ttl_seconds=max(args.dequeue_timeout_seconds * 2 + 60, 120),
            max_tasks=args.max_tasks,
            idle_sleep_seconds=args.idle_sleep_seconds,
            dequeue_timeout_seconds=args.dequeue_timeout_seconds,
        )
        heartbeat_task = _start_runtime_heartbeat("task-worker", runtime_meta)
        try:
            result = await run_task_worker(
                max_tasks=args.max_tasks,
                idle_sleep_seconds=args.idle_sleep_seconds,
                dequeue_timeout_seconds=args.dequeue_timeout_seconds,
            )
            print(json.dumps(jsonable(result.__dict__), ensure_ascii=False, indent=2))
        finally:
            await _stop_runtime_heartbeat(heartbeat_task)
            await engine.dispose()
        return 0 if not result.failed else 2

    if args.command == "telegram":
        client = TelegramClient()
        if args.telegram_command == "status":
            result = await client.status()
            print(json.dumps(jsonable(result.__dict__), ensure_ascii=False, indent=2))
            return 0 if result.configured and not result.error else 2
        if args.telegram_command == "updates":
            updates = await client.get_updates()
            print(
                json.dumps(
                    {"chats": extract_chat_candidates(updates), "update_count": len(updates)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.telegram_command == "send":
            result = await client.send_message(args.text, chat_id=args.chat_id)
            print(json.dumps({"message_id": result.get("message_id")}, ensure_ascii=False, indent=2))
            return 0

    raise ValueError(f"Unsupported command: {args.command}")


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
