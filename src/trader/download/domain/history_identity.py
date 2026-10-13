"""Stable content identity for published history values."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import date, datetime


def history_content_hash(value: object) -> str:
    """Return the existing SHA-256 identity of a history value."""

    return hashlib.sha256(_history_json(value).encode()).hexdigest()


def _history_json(value: object) -> str:
    return json.dumps(
        _history_value(value),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _history_value(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _history_value(getattr(value, field.name)) for field in dataclasses.fields(value) if field.init
        }
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_history_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _history_value(item) for key, item in value.items()}
    return value


__all__ = ["history_content_hash"]
