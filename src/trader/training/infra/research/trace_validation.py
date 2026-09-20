"""Typed validation helpers for research trace JSON boundaries."""

from __future__ import annotations

from typing import cast

from trader.infra.serialization.fields import as_sequence


def object_value(raw: object, label: str) -> dict[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, object], raw)


def list_value(raw: object, label: str) -> list[object]:
    sequence = as_sequence(raw)
    if sequence is None:
        raise ValueError(f"{label} must be a list")
    return sequence


def text(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be text")
    return value


def optional_text(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError("optional text is invalid")
    return raw


def boolean(raw: dict[str, object], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be boolean")
    return value


def optional_boolean(raw: object) -> bool | None:
    if raw is None:
        return None
    if not isinstance(raw, bool):
        raise ValueError("optional boolean is invalid")
    return raw


def integer(raw: dict[str, object], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def number(raw: dict[str, object], key: str) -> float:
    value = raw.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    return float(value)


def optional_number(raw: object) -> float | None:
    if raw is None:
        return None
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ValueError("optional number is invalid")
    return float(raw)


def strings(raw: object, label: str) -> list[str]:
    values = list_value(raw, label)
    if any(not isinstance(value, str) for value in values):
        raise ValueError(f"{label} must contain text")
    return cast(list[str], values)


def text_pairs(raw: object, label: str) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for item in list_value(raw, label):
        values = list_value(item, label)
        if len(values) != 2 or any(not isinstance(value, str) for value in values):
            raise ValueError(f"{label} entries are invalid")
        result.append((cast(str, values[0]), cast(str, values[1])))
    return tuple(result)


def count_pairs(raw: object) -> tuple[tuple[str, int], ...]:
    result: list[tuple[str, int]] = []
    for item in list_value(raw, "filter_aggregates"):
        values = list_value(item, "filter_aggregate")
        if len(values) != 2 or not isinstance(values[0], str) or not isinstance(values[1], int):
            raise ValueError("filter aggregate is invalid")
        result.append((values[0], values[1]))
    return tuple(result)


def score_pairs(raw: object) -> tuple[tuple[str, float | None], ...]:
    result: list[tuple[str, float | None]] = []
    for item in list_value(raw, "score_components"):
        values = list_value(item, "score_component")
        if len(values) != 2 or not isinstance(values[0], str):
            raise ValueError("score component is invalid")
        result.append((values[0], optional_number(values[1])))
    return tuple(result)


def required_score_pairs(raw: object) -> tuple[tuple[str, float], ...]:
    result: list[tuple[str, float]] = []
    for name, value in score_pairs(raw):
        if value is None:
            raise ValueError("required score component cannot be null")
        result.append((name, value))
    return tuple(result)


def optional_number_pairs(raw: object, label: str) -> tuple[tuple[str, float | None], ...]:
    result: list[tuple[str, float | None]] = []
    for item in list_value(raw, label):
        values = list_value(item, label)
        if len(values) != 2 or not isinstance(values[0], str):
            raise ValueError(f"{label} entries are invalid")
        result.append((values[0], optional_number(values[1])))
    return tuple(result)
