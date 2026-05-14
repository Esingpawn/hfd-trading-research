from __future__ import annotations

from datetime import date, datetime


def jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if hasattr(value, "__dict__"):
        return jsonable(value.__dict__)
    return value
