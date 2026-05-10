from datetime import datetime, timezone

from app.infrastructure.raw_store import LocalRawPayloadStore


def test_local_raw_payload_store_writes_compressed_payload(tmp_path) -> None:
    store = LocalRawPayloadStore(tmp_path)

    ref = store.write_json(
        payload={"payload": "value"},
        symbol="BTCUSDT",
        timeframe="short",
        indicator="smart_money_cost",
        snapshot_id="snapshot-1",
        collected_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    path = store.resolve(ref.uri)
    assert path.exists()
    assert ref.compression == "gzip"
    assert ref.bytes > 0
    assert len(ref.sha256) == 64


def test_local_raw_payload_store_reads_compressed_payload(tmp_path) -> None:
    store = LocalRawPayloadStore(tmp_path)
    payload = {"payload": "value", "smart_money_cost": [{"avg_price": 100}]}

    ref = store.write_json(
        payload=payload,
        symbol="BTCUSDT",
        timeframe="short",
        indicator="smart_money_cost",
        snapshot_id="snapshot-1",
        collected_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert store.read_json(ref.uri, compression=ref.compression, sha256=ref.sha256) == payload


def test_local_raw_payload_store_rejects_digest_mismatch(tmp_path) -> None:
    store = LocalRawPayloadStore(tmp_path)
    ref = store.write_json(
        payload={"payload": "value"},
        symbol="BTCUSDT",
        timeframe="short",
        indicator="smart_money_cost",
        snapshot_id="snapshot-1",
        collected_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    try:
        store.read_json(ref.uri, compression=ref.compression, sha256="0" * 64)
    except ValueError as exc:
        assert "sha256 mismatch" in str(exc)
    else:
        raise AssertionError("expected digest mismatch")
