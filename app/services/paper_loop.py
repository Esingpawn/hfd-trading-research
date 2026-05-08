from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PaperLoopDecision:
    process: bool
    mark_seen: bool
    reason: str
    run_id: str | None


def paper_loop_decision(
    collection_run: Any | None,
    last_seen_run_id: str | None,
) -> PaperLoopDecision:
    if collection_run is None:
        return PaperLoopDecision(False, False, "no_collection_run", None)

    run_id = str(getattr(collection_run, "id", "") or "")
    if not run_id:
        return PaperLoopDecision(False, False, "collection_run_missing_id", None)
    if run_id == last_seen_run_id:
        return PaperLoopDecision(False, False, "already_seen", run_id)
    if bool(getattr(collection_run, "dry_run", False)):
        return PaperLoopDecision(False, True, "dry_run_collection", run_id)

    status = str(getattr(collection_run, "status", "") or "")
    if status == "running":
        return PaperLoopDecision(False, False, "collection_running", run_id)
    if status != "completed":
        return PaperLoopDecision(False, True, f"collection_status_{status or 'unknown'}", run_id)
    return PaperLoopDecision(True, True, "ready", run_id)
