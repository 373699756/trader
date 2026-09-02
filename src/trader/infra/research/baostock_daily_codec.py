"""Strict JSON primitives shared by BaoStock daily-history artifacts."""

from __future__ import annotations

import json
from datetime import date
from typing import cast


def encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def json_object(value: str) -> dict[str, object]:
    raw = json.loads(value)
    return object_value(raw, "JSON object")


def json_array(value: str) -> list[object]:
    raw = json.loads(value)
    if not isinstance(raw, list):
        raise TypeError("BaoStock JSON array is invalid")
    return cast(list[object], raw)


def object_value(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"BaoStock {label} is invalid")
    return cast(dict[str, object], value)


def fields(raw: dict[str, object], expected: set[str], label: str) -> None:
    if set(raw) != expected:
        raise ValueError(f"BaoStock {label} fields are invalid")


def string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("BaoStock string value is invalid")
    return value


def boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("BaoStock boolean value is invalid")
    return value


def integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("BaoStock integer value is invalid")
    return value


def number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("BaoStock numeric value is invalid")
    return float(value)


def optional_number(value: object) -> float | None:
    return None if value is None else number(value)


def strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("BaoStock string list is invalid")
    return tuple(value)


def dates(value: object) -> tuple[date, ...]:
    return tuple(date.fromisoformat(item) for item in strings(value))


def pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise TypeError("BaoStock pair list is invalid")
    result: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2 or not all(isinstance(part, str) for part in item):
            raise TypeError("BaoStock pair is invalid")
        result.append((item[0], item[1]))
    return tuple(result)
