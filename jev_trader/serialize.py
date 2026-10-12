"""Turn SDK objects into JSON-ready values."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def to_plain(obj: Any, _seen: set[int] | None = None) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.name
    if isinstance(obj, dict):
        return {str(key): to_plain(value, _seen) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(value, _seen) for value in obj]
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", "replace")

    seen = set() if _seen is None else _seen
    marker = id(obj)
    if marker in seen:
        return None
    seen.add(marker)

    attrs: dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        attrs[name] = to_plain(value, seen)
    if attrs:
        return attrs
    return str(obj)
