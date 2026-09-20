"""Stable identities for recommendation requests and immutable inputs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


def request_fingerprint(request: Mapping[str, object]) -> str:
    payload = json.dumps(
        _normalize_request(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_canonical_value,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_request(request: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in request.items():
        if key in {"codes", "fields"} and isinstance(value, (tuple, list, set, frozenset)):
            normalized[key] = sorted({str(item) for item in value})
        elif isinstance(value, Mapping):
            normalized[key] = _normalize_request(value)
        else:
            normalized[key] = value
    return normalized


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("request identity decimals must be finite")
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("request identity datetime must be timezone-aware")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    raise TypeError(f"unsupported request identity value: {type(value).__name__}")


__all__ = ["request_fingerprint"]
