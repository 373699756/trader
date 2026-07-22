"""Primitive validators shared by snapshot codecs."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime

from trader.domain.review.models import (
    DeepSeekReview,
    RiskRule,
)


def _object(raw: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _object_list(raw: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    value = raw.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{key} must be a list of objects")
    return tuple(value)


def _string_list(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return tuple(value)


def _aware_datetime(raw: Mapping[str, object], key: str) -> datetime:
    value = datetime.fromisoformat(_text(raw, key))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{key} must be timezone-aware")
    return value


def _mapping_key(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("mapping keys must be non-empty strings")
    return value


def _number_mapping(raw: Mapping[str, object]) -> dict[str, float]:
    return {_mapping_key(key): _required_number(value) for key, value in raw.items()}


def _nested_number_mapping(raw: Mapping[str, object]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError("nested number mappings must contain objects")
        result[_mapping_key(key)] = _number_mapping(value)
    return result


def _risk_rule_mapping(raw: Mapping[str, object]) -> dict[str, RiskRule]:
    result: dict[str, RiskRule] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError("risk rule mappings must contain objects")
        code = _mapping_key(key)
        ttl = value.get("evidence_ttl_hours", 876_000)
        veto = value.get("veto", False)
        local_trigger_enabled = value.get("local_trigger_enabled", True)
        evidence_types = value.get("allowed_evidence_types", [])
        strategies = value.get("strategies", [])
        trigger_thresholds = value.get("trigger_thresholds", [])
        fact_id_fields = value.get("risk_fact_id_fields", [])
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1:
            raise ValueError("risk rule evidence_ttl_hours must be a positive integer")
        if not isinstance(veto, bool):
            raise ValueError("risk rule veto must be boolean")
        if not isinstance(local_trigger_enabled, bool):
            raise ValueError("risk rule local_trigger_enabled must be boolean")
        if not isinstance(evidence_types, list) or any(
            not isinstance(item, str) or not item for item in evidence_types
        ):
            raise ValueError("risk rule allowed_evidence_types must be a list of non-empty strings")
        if not isinstance(strategies, list) or any(not isinstance(item, str) for item in strategies):
            raise ValueError("risk rule strategies must be a list of strings")
        if not isinstance(trigger_thresholds, list) or any(
            not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(float(item))
            for item in trigger_thresholds
        ):
            raise ValueError("risk rule trigger_thresholds must be finite numbers")
        if not isinstance(fact_id_fields, list) or any(not isinstance(item, str) for item in fact_id_fields):
            raise ValueError("risk rule risk_fact_id_fields must be a list of strings")
        result[code] = RiskRule(
            risk_code=_text(value, "risk_code"),
            severity=_text(value, "severity"),
            penalty=_number(value, "penalty"),
            minimum_confidence=_number(value, "minimum_confidence"),
            group=_text(value, "group"),
            evidence_ttl_hours=ttl,
            veto=veto,
            allowed_evidence_types=tuple(evidence_types),
            strategies=tuple(strategies),
            trigger_factor=str(value.get("trigger_factor") or ""),
            trigger_operator=str(value.get("trigger_operator") or ""),
            trigger_thresholds=tuple(float(item) for item in trigger_thresholds),
            combination_mode=str(value.get("combination_mode") or "exclusive"),
            risk_fact_id_fields=tuple(fact_id_fields),
            local_trigger_enabled=local_trigger_enabled,
        )
    return result


def _review_mapping(raw: Mapping[str, object]) -> dict[str, DeepSeekReview]:
    from trader.infra.persistence.snapshot_items import _review_from_dict

    result: dict[str, DeepSeekReview] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError("review mappings must contain objects")
        result[_mapping_key(key)] = _review_from_dict(value)
    return result


def _text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_text(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string when present")
    stripped = value.strip()
    return stripped if stripped else None


def _integer(raw: Mapping[str, object], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _number(raw: Mapping[str, object], key: str) -> float:
    value = _optional_number(raw.get(key))
    if value is None:
        raise ValueError(f"{key} must be a number")
    return value


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("expected a number or null")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("numbers must be finite")
    return result


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("expected a non-negative integer or null")
    return value


def _required_number(value: object) -> float:
    result = _optional_number(value)
    if result is None:
        raise ValueError("expected a number")
    return result
