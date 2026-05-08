from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
