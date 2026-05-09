from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api import shared


def test_runtime_process_payload_uses_recent_heartbeat(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(shared, "RUNTIME_DIR", tmp_path)
    (tmp_path / "paper-loop.json").write_text(
        _runtime_meta(heartbeat_at=datetime.now(timezone.utc).isoformat()),
        encoding="utf-8",
    )

    payload = shared._runtime_process_payload("paper-loop", "Paper loop")

    assert payload["running"] is True
    assert payload["heartbeat_age_seconds"] is not None
    assert payload["heartbeat_ttl_seconds"] == 120


def test_runtime_process_payload_rejects_stale_heartbeat(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(shared, "RUNTIME_DIR", tmp_path)
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    (tmp_path / "paper-loop.json").write_text(
        _runtime_meta(heartbeat_at=stale.isoformat(), heartbeat_ttl_seconds=120),
        encoding="utf-8",
    )

    payload = shared._runtime_process_payload("paper-loop", "Paper loop")

    assert payload["running"] is False


def _runtime_meta(*, heartbeat_at: str, heartbeat_ttl_seconds: int | None = None) -> str:
    ttl_line = (
        f', "heartbeat_ttl_seconds": {heartbeat_ttl_seconds}'
        if heartbeat_ttl_seconds is not None
        else ""
    )
    return (
        '{"pid": -1, "started_at": "2026-05-10T00:00:00+00:00", '
        f'"heartbeat_at": "{heartbeat_at}"{ttl_line}}}'
    )
