from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RawPayloadRef:
    uri: str
    sha256: str
    bytes: int
    compression: str


class LocalRawPayloadStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def resolve(self, uri: str) -> Path:
        if not uri.startswith("local://"):
            raise ValueError(f"Unsupported raw payload uri: {uri}")
        relative = uri.removeprefix("local://")
        path = (self.root / relative).resolve()
        root = self.root.resolve()
        if root not in path.parents and path != root:
            raise ValueError(f"Raw payload path escapes root: {uri}")
        return path

    def read_json(
        self,
        uri: str,
        *,
        compression: str | None = "gzip",
        sha256: str | None = None,
    ) -> dict[str, Any]:
        path = self.resolve(uri)
        if compression in (None, "", "none"):
            raw_bytes = path.read_bytes()
        elif compression == "gzip":
            with gzip.open(path, "rb") as handle:
                raw_bytes = handle.read()
        else:
            raise ValueError(f"Unsupported raw payload compression: {compression}")
        if sha256 and hashlib.sha256(raw_bytes).hexdigest() != sha256:
            raise ValueError(f"Raw payload sha256 mismatch: {uri}")
        payload = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Raw payload must be a JSON object: {uri}")
        return payload

    def write_json(
        self,
        *,
        payload: dict[str, Any],
        symbol: str,
        timeframe: str,
        indicator: str,
        snapshot_id: str,
        collected_at: datetime,
    ) -> RawPayloadRef:
        raw_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        relative = Path(
            f"{collected_at:%Y/%m/%d}",
            symbol,
            timeframe,
            indicator,
            f"{snapshot_id}.json.gz",
        )
        path = (self.root / relative).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wb") as handle:
            handle.write(raw_bytes)
        return RawPayloadRef(
            uri=f"local://{relative.as_posix()}",
            sha256=digest,
            bytes=len(raw_bytes),
            compression="gzip",
        )
