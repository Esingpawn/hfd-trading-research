from datetime import datetime, timezone
from types import SimpleNamespace

from app.cli_commands.utils import jsonable


def test_jsonable_serializes_datetimes() -> None:
    value = {"created_at": datetime(2026, 5, 15, 1, 2, 3, tzinfo=timezone.utc)}

    assert jsonable(value) == {"created_at": "2026-05-15T01:02:03+00:00"}


def test_jsonable_serializes_orm_like_objects() -> None:
    value = SimpleNamespace(
        id="run-1",
        created_at=datetime(2026, 5, 15, 1, 2, 3, tzinfo=timezone.utc),
        items={"b", "a"},
    )

    assert jsonable(value) == {
        "id": "run-1",
        "created_at": "2026-05-15T01:02:03+00:00",
        "items": ["a", "b"],
    }
