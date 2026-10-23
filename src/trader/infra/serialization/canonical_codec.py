"""Pure canonical encoding boundary for persistent content identities.

Business modules select their identity fields. This codec owns byte ordering,
dataclass traversal and JSON representation, without configuration or I/O.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import TypeAlias

CanonicalValue: TypeAlias = None | bool | int | float | str | list["CanonicalValue"] | dict[str, "CanonicalValue"]


@dataclass(frozen=True)
class CanonicalExclusion:
    identity_field: str
    identity: str
    metadata_field: str


def EncodeCanonicalValue(
    value: object, *, include_derived: bool = False, exclusion: CanonicalExclusion | None = None
) -> CanonicalValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        excluded = exclusion is not None and getattr(value, exclusion.identity_field, None) == exclusion.identity
        return {
            field.name: EncodeCanonicalValue(
                getattr(value, field.name), include_derived=include_derived, exclusion=exclusion
            )
            for field in dataclasses.fields(value)
            if (include_derived or field.init)
            and not (excluded and exclusion is not None and field.metadata.get(exclusion.metadata_field, False))
        }
    if isinstance(value, Mapping):
        return {
            str(key): EncodeCanonicalValue(item, include_derived=include_derived, exclusion=exclusion)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list, frozenset, set)):
        ordered = sorted(value) if isinstance(value, (frozenset, set)) else value
        return [EncodeCanonicalValue(item, include_derived=include_derived, exclusion=exclusion) for item in ordered]
    raise TypeError(f"unsupported canonical identity value: {type(value).__name__}")


def CanonicalText(
    value: object,
    *,
    ensure_ascii: bool = True,
    include_derived: bool = False,
    exclusion: CanonicalExclusion | None = None,
) -> str:
    return CanonicalJsonText(
        EncodeCanonicalValue(value, include_derived=include_derived, exclusion=exclusion), ensure_ascii=ensure_ascii
    )


def CanonicalJsonText(value: CanonicalValue, *, ensure_ascii: bool = True) -> str:
    """Encode a persistence codec's explicit JSON projection without reflective traversal."""
    return json.dumps(
        value,
        ensure_ascii=ensure_ascii,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def CanonicalDigest(
    value: object,
    *,
    ensure_ascii: bool = True,
    include_derived: bool = False,
    exclusion: CanonicalExclusion | None = None,
) -> str:
    return hashlib.sha256(
        CanonicalText(value, ensure_ascii=ensure_ascii, include_derived=include_derived, exclusion=exclusion).encode(
            "utf-8"
        )
    ).hexdigest()


__all__ = [
    "CanonicalExclusion",
    "CanonicalValue",
    "EncodeCanonicalValue",
    "CanonicalText",
    "CanonicalJsonText",
    "CanonicalDigest",
]
