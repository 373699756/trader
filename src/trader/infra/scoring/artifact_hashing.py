"""Canonical hashing for production scoring artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def artifact_content_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["artifact_content_hash"]
