"""Immutable, source-specific market-data observations."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Literal
from zoneinfo import ZoneInfo

JsonScalar = str | float | bool | None
ObservationStatus = Literal["success", "no_data", "failed", "late"]


@dataclass(frozen=True)
class SourceObservation:
    source: str
    subject_key: str
    observed_at: datetime
    source_time: datetime
    received_at: datetime
    effective_at: datetime
    data_version: str
    fields: Mapping[str, JsonScalar]
    missing_reasons: Mapping[str, str]
    payload_hash: str
    status: ObservationStatus
    error_code: str | None
    trade_date: date | None = None
    observation_point: datetime | None = None
    security_identity: str = ""

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.subject_key.strip():
            raise ValueError("observation source and subject_key must not be empty")
        for name, time_value in (
            ("observed_at", self.observed_at),
            ("source_time", self.source_time),
            ("received_at", self.received_at),
            ("effective_at", self.effective_at),
        ):
            if time_value.tzinfo is None or time_value.utcoffset() is None:
                raise ValueError(f"observation {name} must be timezone-aware")
        if self.trade_date is None:
            object.__setattr__(self, "trade_date", self.observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date())
        elif not isinstance(self.trade_date, date):
            raise TypeError("observation trade_date must be a date")
        if self.observation_point is None:
            object.__setattr__(self, "observation_point", self.observed_at)
        elif self.observation_point.tzinfo is None or self.observation_point.utcoffset() is None:
            raise ValueError("observation observation_point must be timezone-aware")
        if not self.security_identity:
            object.__setattr__(self, "security_identity", self.subject_key)
        if not self.security_identity.strip():
            raise ValueError("observation security_identity must not be empty")
        if self.status not in {"success", "no_data", "failed", "late"}:
            raise ValueError("unsupported observation status")
        normalized_fields = _normalize_fields(self.fields)
        missing = {str(key): str(value) for key, value in self.missing_reasons.items()}
        if any(not key or not value for key, value in missing.items()):
            raise ValueError("observation missing reasons must not be empty")
        object.__setattr__(self, "fields", MappingProxyType(normalized_fields))
        object.__setattr__(self, "missing_reasons", MappingProxyType(missing))


def _normalize_fields(fields: Mapping[str, JsonScalar]) -> dict[str, JsonScalar]:
    normalized: dict[str, JsonScalar] = {}
    for key, field_value in fields.items():
        if not isinstance(key, str) or not key:
            raise ValueError("observation field names must not be empty")
        if field_value is None or isinstance(field_value, (str, bool)):
            normalized[key] = field_value
            continue
        if isinstance(field_value, (int, float)) and not isinstance(field_value, bool):
            number = float(field_value)
            if not math.isfinite(number):
                raise ValueError("observation numeric fields must be finite")
            normalized[key] = number
            continue
        raise TypeError("observation fields must contain JSON scalars")
    return normalized


__all__ = ["JsonScalar", "ObservationStatus", "SourceObservation"]
