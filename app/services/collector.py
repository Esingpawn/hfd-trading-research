from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import ASSETS, COLLECTABLE_INDICATORS, CORE_INDICATORS, TIMEFRAMES
from app.hfd.client import HfdClient
from app.infrastructure.raw_store import LocalRawPayloadStore
from app.models import CollectionRun, PriceSnapshot, SignalSnapshot
from app.services.features import summarize_signal_payload


@dataclass
class CollectionResult:
    run_id: str | None
    status: str
    dry_run: bool
    assets: list[str]
    timeframes: list[str]
    indicators: list[str]
    snapshots_written: int = 0
    prices_written: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


def normalize_assets(values: Iterable[str] | None) -> list[str]:
    selected = [v.upper() for v in values] if values else list(ASSETS.keys())
    unknown = sorted(set(selected) - set(ASSETS))
    if unknown:
        raise ValueError(f"Unknown assets: {', '.join(unknown)}")
    return selected


def normalize_timeframes(values: Iterable[str] | None) -> list[str]:
    selected = [v.lower() for v in values] if values else list(TIMEFRAMES.keys())
    unknown = sorted(set(selected) - set(TIMEFRAMES))
    if unknown:
        raise ValueError(f"Unknown timeframes: {', '.join(unknown)}")
    return selected


def normalize_indicators(values: Iterable[str] | None) -> list[str]:
    selected = list(values) if values else list(CORE_INDICATORS)
    unknown = sorted(set(selected) - set(COLLECTABLE_INDICATORS))
    if unknown:
        raise ValueError(f"Unsupported indicators for collection: {', '.join(unknown)}")
    return selected


class SnapshotCollector:
    def __init__(self, session: AsyncSession, client: HfdClient | None = None) -> None:
        self.session = session
        self.client = client or HfdClient()
        self._owns_client = client is None
        self.settings = get_settings()
        self.raw_store = LocalRawPayloadStore(self.settings.raw_payload_dir)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.close()

    async def collect(
        self,
        assets: Iterable[str] | None = None,
        timeframes: Iterable[str] | None = None,
        indicators: Iterable[str] | None = None,
        dry_run: bool = False,
    ) -> CollectionResult:
        selected_assets = normalize_assets(assets)
        selected_timeframes = normalize_timeframes(timeframes)
        selected_indicators = normalize_indicators(indicators)

        run = CollectionRun(
            status="running",
            dry_run=dry_run,
            requested_assets=selected_assets,
            requested_timeframes=selected_timeframes,
            requested_indicators=selected_indicators,
            errors=[],
        )
        if not dry_run:
            self.session.add(run)
            await self.session.flush()

        result = CollectionResult(
            run_id=run.id if not dry_run else None,
            status="running",
            dry_run=dry_run,
            assets=selected_assets,
            timeframes=selected_timeframes,
            indicators=selected_indicators,
        )

        collected_at = datetime.now(timezone.utc)

        for coin in selected_assets:
            await self._collect_price(coin, collected_at, dry_run, result)
            for timeframe_name in selected_timeframes:
                interval = TIMEFRAMES[timeframe_name].interval
                for indicator in selected_indicators:
                    await self._collect_signal(
                        coin,
                        timeframe_name,
                        interval,
                        indicator,
                        collected_at,
                        dry_run,
                        result,
                    )

        result.status = "completed" if not result.errors else "completed_with_errors"
        if not dry_run:
            run.status = result.status
            run.snapshots_written = result.snapshots_written
            run.prices_written = result.prices_written
            run.errors = result.errors
            run.finished_at = datetime.now(timezone.utc)
            await self.session.commit()
        return result

    async def _collect_price(
        self,
        coin: str,
        collected_at: datetime,
        dry_run: bool,
        result: CollectionResult,
    ) -> None:
        try:
            payload = await self.client.fetch_price(coin)
            if dry_run:
                result.prices_written += 1
                return
            self.session.add(
                PriceSnapshot(
                    symbol=f"{coin}USDT",
                    price=float(payload["price"]),
                    raw_payload=payload,
                    collected_at=collected_at,
                )
            )
            result.prices_written += 1
        except Exception as exc:  # noqa: BLE001
            result.errors.append({"coin": coin, "stage": "price", "error": str(exc)})

    async def _collect_signal(
        self,
        coin: str,
        timeframe_name: str,
        interval: str,
        indicator: str,
        collected_at: datetime,
        dry_run: bool,
        result: CollectionResult,
    ) -> None:
        try:
            payload = await self.client.fetch_pro_data(coin, interval, indicator)
            if dry_run:
                result.snapshots_written += 1
                return
            endpoint = (
                f"/api/pro/pro_data?coin={coin}&interval={interval}"
                f"&indicator={indicator}"
            )
            raw_payload = payload
            raw_ref = None
            snapshot_id = None
            if self.settings.externalize_raw_payloads:
                from app.models import uuid_str

                snapshot_id = uuid_str()
                raw_ref = self.raw_store.write_json(
                    payload=payload,
                    symbol=f"{coin}USDT",
                    timeframe=timeframe_name,
                    indicator=indicator,
                    snapshot_id=snapshot_id,
                    collected_at=collected_at,
                )
                raw_payload = {}
            snapshot_values = {
                "symbol": f"{coin}USDT",
                "asset_tier": ASSETS[coin].tier,
                "timeframe": timeframe_name,
                "interval": interval,
                "indicator": indicator,
                "endpoint": endpoint,
                "raw_payload": raw_payload,
                "raw_payload_uri": raw_ref.uri if raw_ref else None,
                "raw_payload_sha256": raw_ref.sha256 if raw_ref else None,
                "raw_payload_bytes": raw_ref.bytes if raw_ref else None,
                "raw_payload_compression": raw_ref.compression if raw_ref else None,
                "summary_payload": summarize_signal_payload(payload, indicator),
                "collected_at": collected_at,
            }
            if snapshot_id:
                snapshot_values["id"] = snapshot_id
            self.session.add(
                SignalSnapshot(**snapshot_values)
            )
            result.snapshots_written += 1
        except Exception as exc:  # noqa: BLE001
            result.errors.append(
                {
                    "coin": coin,
                    "timeframe": timeframe_name,
                    "interval": interval,
                    "indicator": indicator,
                    "error": str(exc),
                }
            )
