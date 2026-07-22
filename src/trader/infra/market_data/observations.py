"""Immutable, source-specific market-data observations."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal

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
        if self.status not in {"success", "no_data", "failed", "late"}:
            raise ValueError("unsupported observation status")
        normalized_fields: dict[str, JsonScalar] = {}
        for key, field_value in self.fields.items():
            if not isinstance(key, str) or not key:
                raise ValueError("observation field names must not be empty")
            if field_value is None or isinstance(field_value, (str, bool)):
                normalized_fields[key] = field_value
                continue
            if isinstance(field_value, (int, float)) and not isinstance(field_value, bool):
                number = float(field_value)
                if not math.isfinite(number):
                    raise ValueError("observation numeric fields must be finite")
                normalized_fields[key] = number
                continue
            raise TypeError("observation fields must contain JSON scalars")
        missing = {str(key): str(value) for key, value in self.missing_reasons.items()}
        if any(not key or not value for key, value in missing.items()):
            raise ValueError("observation missing reasons must not be empty")
        object.__setattr__(self, "fields", MappingProxyType(normalized_fields))
        object.__setattr__(self, "missing_reasons", MappingProxyType(missing))


__all__ = ["JsonScalar", "ObservationStatus", "SourceObservation"]
