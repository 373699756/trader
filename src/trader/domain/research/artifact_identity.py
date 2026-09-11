"""Canonical serialization and content identity for research artifacts."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import date, datetime

from trader.domain.research.specification import HISTORICAL_RESEARCH_SPEC


def canonical_artifact_hash(value: object) -> str:
    """Return the stable SHA-256 identity of a research artifact value."""

    return hashlib.sha256(canonical_artifact_json(value).encode()).hexdigest()


def canonical_artifact_json(value: object) -> str:
    """Render a research artifact using its stable canonical JSON form."""

    return json.dumps(
        canonical_artifact_value(value),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_artifact_value(value: object) -> object:
    """Project a typed research value into canonical serialization primitives."""

    if dataclasses.is_dataclass(value):
        preserves_historical_identity = (
            getattr(value, "research_identity", None) == HISTORICAL_RESEARCH_SPEC.research_identity
        )
        return {
            field.name: canonical_artifact_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if field.init and not (preserves_historical_identity and field.metadata.get("exclude_from_v1_hash", False))
        }
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [canonical_artifact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): canonical_artifact_value(item) for key, item in value.items()}
    return value


__all__ = ["canonical_artifact_hash", "canonical_artifact_json", "canonical_artifact_value"]
