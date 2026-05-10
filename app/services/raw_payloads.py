from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.config import get_settings
from app.infrastructure.raw_store import LocalRawPayloadStore


@lru_cache(maxsize=8)
def _store_for_root(root: str) -> LocalRawPayloadStore:
    return LocalRawPayloadStore(root)


def payload_for_snapshot(
    snapshot: Any,
    *,
    store: LocalRawPayloadStore | None = None,
) -> dict[str, Any]:
    payload = getattr(snapshot, "raw_payload", None) or {}
    if isinstance(payload, dict) and payload:
        return payload

    uri = getattr(snapshot, "raw_payload_uri", None)
    if not uri:
        return {}

    compression = getattr(snapshot, "raw_payload_compression", None) or "gzip"
    sha256 = getattr(snapshot, "raw_payload_sha256", None)
    reader = store or _store_for_root(get_settings().raw_payload_dir)
    try:
        return reader.read_json(uri, compression=compression, sha256=sha256)
    except (OSError, ValueError):
        return {}
