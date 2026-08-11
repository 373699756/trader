"""Field-level market quality domain models for deterministic merge.

The v2 data plane carries field provenance and quality at per-field granularity.
These structures intentionally stay immutable and hash-friendly so they can be used
for audit and deterministic projection checks in later phases.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType

JsonScalar = str | float | bool | None


class FieldQualityState(str, Enum):
    """Allowed per-field quality states."""

    VALID = "valid"
    DEGRADED = "degraded"
    STALE = "stale"
    MISSING = "missing"
    CONFLICTING = "conflicting"


def _frozen_mapping(values: Mapping[str, FieldValue]) -> Mapping[str, FieldValue]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class FieldValue:
    name: str
    value: JsonScalar
    source: str
    source_time: datetime
    received_time: datetime
    data_version: str
    payload_hash: str
    quality: FieldQualityState = FieldQualityState.VALID
    conflict_count: int = 0

    def __post_init__(self) -> None:
        self._validate_identity()
        self._validate_timestamps()
        self._validate_content()

    def _validate_identity(self) -> None:
        if not self.name.strip():
            raise ValueError("field name must not be empty")
        if not self.source.strip():
            raise ValueError("field source must not be empty")

    def _validate_timestamps(self) -> None:
        if self.source_time.tzinfo is None or self.source_time.utcoffset() is None:
            raise ValueError("field source_time must be timezone-aware")
        if self.received_time.tzinfo is None or self.received_time.utcoffset() is None:
            raise ValueError("field received_time must be timezone-aware")
        if self.received_time < self.source_time:
            raise ValueError("field received_time cannot precede source_time")

    def _validate_content(self) -> None:
        if not isinstance(self.quality, FieldQualityState):
            raise TypeError("field quality must be a FieldQualityState")
        if not self.data_version.strip():
            raise ValueError("field data_version must not be empty")
        if not self.payload_hash.strip():
            raise ValueError("field payload_hash must not be empty")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("field value must be finite when numeric")
        if not isinstance(self.conflict_count, int) or isinstance(self.conflict_count, bool):
            raise TypeError("field conflict_count must be an integer")
        if self.conflict_count < 0:
            raise ValueError("field conflict_count cannot be negative")
        if self.quality is FieldQualityState.MISSING and self.value is not None:
            raise ValueError("missing field quality requires a null value")


@dataclass(frozen=True)
class SecurityMaster:
    board: FieldValue | None = None
    exchange: FieldValue | None = None
    listing_date: FieldValue | None = None
    listing_age_sessions: FieldValue | None = None
    is_relisted_first_session: FieldValue | None = None
    is_delisting_period_first_session: FieldValue | None = None
    has_price_limit: FieldValue | None = None
    exchange_limit_pct: FieldValue | None = None
    strategy_hot_cap_pct: FieldValue | None = None
    board_reliability: FieldValue | None = None
    rule_version: FieldValue | None = None
    rule_effective_date: FieldValue | None = None
    extended: Mapping[str, FieldValue] = field(default_factory=lambda: _frozen_mapping({}))

    def values(self) -> Mapping[str, FieldValue]:
        return _frozen_mapping(
            {
                name: value
                for name, value in (
                    ("board", self.board),
                    ("exchange", self.exchange),
                    ("listing_date", self.listing_date),
                    ("listing_age_sessions", self.listing_age_sessions),
                    ("is_relisted_first_session", self.is_relisted_first_session),
                    ("is_delisting_period_first_session", self.is_delisting_period_first_session),
                    ("has_price_limit", self.has_price_limit),
                    ("exchange_limit_pct", self.exchange_limit_pct),
                    ("strategy_hot_cap_pct", self.strategy_hot_cap_pct),
                    ("board_reliability", self.board_reliability),
                    ("rule_version", self.rule_version),
                    ("rule_effective_date", self.rule_effective_date),
                )
                if value is not None
            }
            | dict(self.extended)
        )


@dataclass(frozen=True)
class RealtimeQuote:
    name: FieldValue | None = None
    price: FieldValue | None = None
    previous_close: FieldValue | None = None
    open_price: FieldValue | None = None
    high: FieldValue | None = None
    low: FieldValue | None = None
    pct_change: FieldValue | None = None
    change_5m: FieldValue | None = None
    speed: FieldValue | None = None
    volume_ratio: FieldValue | None = None
    turnover_rate: FieldValue | None = None
    amount: FieldValue | None = None
    amplitude: FieldValue | None = None
    market_cap: FieldValue | None = None
    is_st: FieldValue | None = None
    is_suspended: FieldValue | None = None
    is_one_price_limit: FieldValue | None = None
    is_blacklisted: FieldValue | None = None
    has_major_regulatory_risk: FieldValue | None = None
    extended: Mapping[str, FieldValue] = field(default_factory=lambda: _frozen_mapping({}))

    def values(self) -> Mapping[str, FieldValue]:
        return _frozen_mapping(
            {
                name: value
                for name, value in (
                    ("name", self.name),
                    ("price", self.price),
                    ("previous_close", self.previous_close),
                    ("open_price", self.open_price),
                    ("high", self.high),
                    ("low", self.low),
                    ("pct_change", self.pct_change),
                    ("change_5m", self.change_5m),
                    ("speed", self.speed),
                    ("volume_ratio", self.volume_ratio),
                    ("turnover_rate", self.turnover_rate),
                    ("amount", self.amount),
                    ("amplitude", self.amplitude),
                    ("market_cap", self.market_cap),
                    ("is_st", self.is_st),
                    ("is_suspended", self.is_suspended),
                    ("is_one_price_limit", self.is_one_price_limit),
                    ("is_blacklisted", self.is_blacklisted),
                    ("has_major_regulatory_risk", self.has_major_regulatory_risk),
                )
                if value is not None
            }
            | dict(self.extended)
        )


@dataclass(frozen=True)
class HistoricalFeature:
    values: Mapping[str, FieldValue] = field(default_factory=lambda: _frozen_mapping({}))


@dataclass(frozen=True)
class IntradayFeature:
    values: Mapping[str, FieldValue] = field(default_factory=lambda: _frozen_mapping({}))


@dataclass(frozen=True)
class RiskEvidence:
    values: Mapping[str, FieldValue] = field(default_factory=lambda: _frozen_mapping({}))


__all__ = [
    "FieldQualityState",
    "FieldValue",
    "SecurityMaster",
    "RealtimeQuote",
    "HistoricalFeature",
    "RiskEvidence",
    "IntradayFeature",
]
