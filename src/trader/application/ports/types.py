"""Immutable JSON-shaped values used only at observability boundaries."""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = "JsonScalar | tuple[JsonValue, ...] | JsonObject"
JsonObject: TypeAlias = Mapping[str, JsonValue]
JsonInput: TypeAlias = "JsonScalar | list[JsonInput] | tuple[JsonInput, ...] | Mapping[str, JsonInput]"


def freeze_json_object(value: Mapping[str, JsonInput]) -> JsonObject:
    return MappingProxyType({str(key): _freeze_json_value(item) for key, item in value.items()})


def _freeze_json_value(value: JsonInput) -> JsonValue:
    if isinstance(value, Mapping):
        return freeze_json_object(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON floats must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def thaw_json_value(value: JsonValue) -> JsonInput:
    if isinstance(value, Mapping):
        return {key: thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json_value(item) for item in value]
    return value
