"""Pure canonical encoding boundary for persistent content identities.

Business modules select their identity fields. This codec owns byte ordering,
dataclass traversal and JSON representation, without configuration or I/O.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from datetime import date
from enum import Enum
from typing import TypeAlias

CanonicalValue: TypeAlias = None | bool | int | float | str | list["CanonicalValue"] | dict[str, "CanonicalValue"]


def EncodeCanonicalValue(value: object, *, include_derived: bool = False) -> CanonicalValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: EncodeCanonicalValue(getattr(value, field.name), include_derived=include_derived)
            for field in dataclasses.fields(value)
            if (include_derived or field.init)
        }
    if isinstance(value, Mapping):
        return {
            str(key): EncodeCanonicalValue(item, include_derived=include_derived)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list, frozenset, set)):
        ordered = sorted(value) if isinstance(value, (frozenset, set)) else value
        return [EncodeCanonicalValue(item, include_derived=include_derived) for item in ordered]
    raise TypeError(f"unsupported canonical identity value: {type(value).__name__}")


def CanonicalText(
    value: object,
    *,
    ensure_ascii: bool = True,
    include_derived: bool = False,
) -> str:
    return CanonicalJsonText(
        EncodeCanonicalValue(value, include_derived=include_derived), ensure_ascii=ensure_ascii
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
) -> str:
    return hashlib.sha256(
        CanonicalText(value, ensure_ascii=ensure_ascii, include_derived=include_derived).encode(
            "utf-8"
        )
    ).hexdigest()


__all__ = [
    "CanonicalValue",
    "EncodeCanonicalValue",
    "CanonicalText",
    "CanonicalJsonText",
    "CanonicalDigest",
]
