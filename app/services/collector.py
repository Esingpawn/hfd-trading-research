from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import ASSETS, COLLECTABLE_INDICATORS, CORE_INDICATORS, TIMEFRAMES
from app.hfd.client import HfdClient
from app.infrastructure.raw_store import LocalRawPayloadStore
from app.infrastructure.raw_store import RawPayloadRef
from app.models import CollectionRun, PriceSnapshot, SignalSnapshot, uuid_str
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


@dataclass(frozen=True)
class CollectedPrice:
    coin: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class CollectedSignal:
    coin: str
    timeframe_name: str
    interval: str
    indicator: str
    payload: dict[str, Any]
    summary_payload: dict[str, Any]
    snapshot_id: str | None = None
    raw_ref: RawPayloadRef | None = None


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
        result = CollectionResult(
            run_id=None,
            status="running",
            dry_run=dry_run,
            assets=selected_assets,
            timeframes=selected_timeframes,
            indicators=selected_indicators,
        )

        collected_at = datetime.now(timezone.utc)
        prices: list[CollectedPrice] = []
        signals: list[CollectedSignal] = []

        for coin in selected_assets:
            price = await self._fetch_price(coin, result)
            if price is not None:
                prices.append(price)
            for timeframe_name in selected_timeframes:
                interval = TIMEFRAMES[timeframe_name].interval
                for indicator in selected_indicators:
                    signal = await self._fetch_signal(
                        coin,
                        timeframe_name,
                        interval,
                        indicator,
                        collected_at,
                        result,
                    )
                    if signal is not None:
                        signals.append(signal)

        if dry_run:
            result.status = "completed" if not result.errors else "completed_with_errors"
        else:
            self.session.add(run)
            await self.session.flush()
            result.run_id = run.id
            self._store_prices(prices, collected_at, result)
            self._store_signals(signals, collected_at, result)
            result.status = "completed" if not result.errors else "completed_with_errors"
            run.status = result.status
            run.snapshots_written = result.snapshots_written
            run.prices_written = result.prices_written
            run.errors = result.errors
            run.finished_at = datetime.now(timezone.utc)
            await self.session.commit()
        return result

    async def collect_prices_only(
        self,
        assets: Iterable[str] | None = None,
        dry_run: bool = False,
    ) -> CollectionResult:
        selected_assets = normalize_assets(assets)
        run = CollectionRun(
            status="running",
            dry_run=dry_run,
            requested_assets=selected_assets,
            requested_timeframes=[],
            requested_indicators=[],
            errors=[],
        )
        result = CollectionResult(
            run_id=None,
            status="running",
            dry_run=dry_run,
            assets=selected_assets,
            timeframes=[],
            indicators=[],
        )

        collected_at = datetime.now(timezone.utc)
        prices: list[CollectedPrice] = []
        for coin in selected_assets:
            price = await self._fetch_price(coin, result)
            if price is not None:
                prices.append(price)

        if dry_run:
            result.status = "completed" if not result.errors else "completed_with_errors"
        else:
            self.session.add(run)
            await self.session.flush()
            result.run_id = run.id
            self._store_prices(prices, collected_at, result)
            result.status = "completed" if not result.errors else "completed_with_errors"
            run.status = result.status
            run.snapshots_written = 0
            run.prices_written = result.prices_written
            run.errors = result.errors
            run.finished_at = datetime.now(timezone.utc)
            await self.session.commit()
        return result

    async def _fetch_price(
        self,
        coin: str,
        result: CollectionResult,
    ) -> CollectedPrice | None:
        try:
            payload = await self.client.fetch_price(coin)
            if result.dry_run:
                result.prices_written += 1
            return CollectedPrice(coin=coin, payload=payload)
        except Exception as exc:  # noqa: BLE001
            result.errors.append({"coin": coin, "stage": "price", "error": str(exc)})
            return None

    async def _fetch_signal(
        self,
        coin: str,
        timeframe_name: str,
        interval: str,
        indicator: str,
        collected_at: datetime,
        result: CollectionResult,
    ) -> CollectedSignal | None:
        try:
            payload = await self.client.fetch_pro_data(coin, interval, indicator)
            summary_payload = summarize_signal_payload(payload, indicator)
            if result.dry_run:
                result.snapshots_written += 1
            snapshot_id = None
            raw_ref = None
            if self.settings.externalize_raw_payloads and not result.dry_run:
                snapshot_id = uuid_str()
                raw_ref = self.raw_store.write_json(
                    payload=payload,
                    symbol=f"{coin}USDT",
                    timeframe=timeframe_name,
                    indicator=indicator,
                    snapshot_id=snapshot_id,
                    collected_at=collected_at,
                )
                payload = {}
            return CollectedSignal(
                coin=coin,
                timeframe_name=timeframe_name,
                interval=interval,
                indicator=indicator,
                payload=payload,
                summary_payload=summary_payload,
                snapshot_id=snapshot_id,
                raw_ref=raw_ref,
            )
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
            return None

    def _store_prices(
        self,
        prices: list[CollectedPrice],
        collected_at: datetime,
        result: CollectionResult,
    ) -> None:
        for item in prices:
            try:
                self.session.add(
                    PriceSnapshot(
                        symbol=f"{item.coin}USDT",
                        price=float(item.payload["price"]),
                        raw_payload=item.payload,
                        collected_at=collected_at,
                    )
                )
                result.prices_written += 1
            except Exception as exc:  # noqa: BLE001
                result.errors.append({"coin": item.coin, "stage": "price_store", "error": str(exc)})

    def _store_signals(
        self,
        signals: list[CollectedSignal],
        collected_at: datetime,
        result: CollectionResult,
    ) -> None:
        for item in signals:
            try:
                endpoint = (
                    f"/api/pro/pro_data?coin={item.coin}&interval={item.interval}"
                    f"&indicator={item.indicator}"
                )
                raw_ref = item.raw_ref
                raw_payload = {} if raw_ref else item.payload
                snapshot_values = {
                    "symbol": f"{item.coin}USDT",
                    "collection_run_id": result.run_id,
                    "asset_tier": ASSETS[item.coin].tier,
                    "timeframe": item.timeframe_name,
                    "interval": item.interval,
                    "indicator": item.indicator,
                    "endpoint": endpoint,
                    "raw_payload": raw_payload,
                    "raw_payload_uri": raw_ref.uri if raw_ref else None,
                    "raw_payload_sha256": raw_ref.sha256 if raw_ref else None,
                    "raw_payload_bytes": raw_ref.bytes if raw_ref else None,
                    "raw_payload_compression": raw_ref.compression if raw_ref else None,
                    "summary_payload": item.summary_payload,
                    "collected_at": collected_at,
                }
                if item.snapshot_id:
                    snapshot_values["id"] = item.snapshot_id
                self.session.add(
                    SignalSnapshot(**snapshot_values)
                )
                result.snapshots_written += 1
            except Exception as exc:  # noqa: BLE001
                result.errors.append(
                    {
                        "coin": item.coin,
                        "timeframe": item.timeframe_name,
                        "interval": item.interval,
                        "indicator": item.indicator,
                        "stage": "signal_store",
                        "error": str(exc),
                    }
                )
