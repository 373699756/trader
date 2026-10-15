"""Canonical JSON rendering and content identity for published values.

Published values carry a content hash computed from a canonical JSON
rendering: ASCII-only, sorted keys, no insignificant whitespace and no
non-finite numbers. Keeping that rendering in one neutral module is what lets
the scoring and research packages share content identity without importing
each other.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from pathlib import Path

_READ_CHUNK_SIZE = 1024 * 1024


def CanonicalValue(value: object) -> object:
    """Project a typed value into canonical serialization primitives."""

    if isinstance(value, Mapping):
        return {str(key): CanonicalValue(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [CanonicalValue(item) for item in value]
    if isinstance(value, Enum):
        return CanonicalValue(value.value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: CanonicalValue(getattr(value, field.name)) for field in dataclasses.fields(value)}
    return value


def CanonicalJsonText(value: object, *, ascii_only: bool = True) -> str:
    """Render a value using the canonical JSON form used for content identity."""

    return json.dumps(
        CanonicalValue(value),
        ensure_ascii=ascii_only,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def CanonicalJsonBytes(value: object, *, ascii_only: bool = True) -> bytes:
    """Render a value using the canonical JSON form encoded as UTF-8 bytes."""

    return CanonicalJsonText(value, ascii_only=ascii_only).encode("utf-8")


def ContentHash(value: object) -> str:
    """Return the stable SHA-256 identity of a canonicalized value."""

    return hashlib.sha256(CanonicalJsonText(value).encode()).hexdigest()


def FileSha256(path: Path) -> str:
    """Return the SHA-256 digest of a file without loading it whole."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_READ_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CanonicalJsonBytes",
    "CanonicalJsonText",
    "CanonicalValue",
    "ContentHash",
    "FileSha256",
]
