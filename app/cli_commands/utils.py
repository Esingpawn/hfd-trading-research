from __future__ import annotations

from datetime import date, datetime
from enum import Enum


def jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [jsonable(item) for item in value]
    if isinstance(value, set):
        return [jsonable(item) for item in sorted(value, key=str)]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return jsonable(value.__dict__)
    return value
