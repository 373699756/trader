"""Immutable, content-addressed epochs for the tomorrow data plane."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Literal, Protocol, TypeAlias

from trader.domain.market.models import LiveQuote, MarketQuote
from trader.domain.market.quality import FieldQualityState, FieldValue
from trader.domain.market.research import ResearchObservation

DAILY_FEATURE_PACK_SCHEMA_VERSION = "daily_feature_pack_v2"
MARKET_EPOCH_SCHEMA_VERSION = "market_epoch_v1"
CANDIDATE_QUOTE_EPOCH_SCHEMA_VERSION = "candidate_quote_epoch_v2"
RESEARCH_EPOCH_SCHEMA_VERSION = "research_epoch_v1"
CORE_HISTORY_MIN_SESSIONS = 20

_SHANGHAI_TIMEZONE = "Asia/Shanghai"
_REASON_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
CANDIDATE_REALTIME_FEATURES = frozenset(
    {
        "breakout_deviation_pct",
        "capacity_score",
        "close_location",
        "entry_quality",
        "intraday_reversal",
        "limit_distance_safety",
        "liquidity_contraction",
        "moderate_amplitude",
        "price_executability",
        "short_term_overheat",
        "tail_return_30m",
        "tail_return_30m_pct",
        "tail_volume_ratio",
        "tail_volume_ratio_raw",
        "trend_breakdown",
        "volume_to_5d_average",
    }
)
_CanonicalValue: TypeAlias = str | int | float | bool | None | list["_CanonicalValue"] | dict[str, "_CanonicalValue"]
_MARKET_REQUIRED_LINEAGE_FIELDS = frozenset(
    {
        "amount",
        "board",
        "exchange",
        "high",
        "listing_age_sessions",
        "listing_date",
        "low",
        "name",
        "open_price",
        "pct_change",
        "previous_close",
        "price",
    }
)
_CANDIDATE_REQUIRED_LINEAGE_FIELDS = frozenset(
    {"cross_source_deviation_pct", "cross_source_verified", "pct_change", "price"}
)
_RESEARCH_REQUIRED_LINEAGE_FIELDS = frozenset({"announcements", "corporate_risk", "financial", "pledge", "unlock"})


class _EpochCoordinates(Protocol):
    @property
    def trade_date(self) -> date: ...

    @property
    def sequence(self) -> int: ...

    @property
    def observed_at(self) -> datetime: ...

    @property
    def received_at(self) -> datetime: ...

    @property
    def config_version(self) -> str: ...

    @property
    def schema_version(self) -> str: ...


@dataclass(frozen=True)
class DataPlaneCoverage:
    potential_executable_codes: tuple[str, ...]
    security_master_codes: tuple[str, ...]
    candidate_codes: tuple[str, ...]
    candidate_history_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "potential_executable_codes",
            "security_master_codes",
            "candidate_codes",
            "candidate_history_codes",
        ):
            normalized = _sorted_unique_codes(getattr(self, name), name)
            object.__setattr__(self, name, normalized)
        if not set(self.potential_executable_codes) <= set(self.security_master_codes):
            raise ValueError("potential executable security-master coverage must be 100%")
        if not set(self.candidate_history_codes) <= set(self.candidate_codes):
            raise ValueError("candidate history coverage codes must be candidates")

    @property
    def security_master_coverage(self) -> float:
        if not self.potential_executable_codes:
            return 1.0
        covered = len(set(self.potential_executable_codes).intersection(self.security_master_codes))
        return covered / len(self.potential_executable_codes)

    @property
    def candidate_history_coverage(self) -> float:
        if not self.candidate_codes:
            return 1.0
        return len(self.candidate_history_codes) / len(self.candidate_codes)


@dataclass(frozen=True)
class DailyFeatureRow:
    code: str
    values: Mapping[str, float | None]
    history_sessions: int
    data_as_of: date
    field_values: Mapping[str, FieldValue]
    security_master_version: str = ""
    history_version: str = ""
    risk_component_versions: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    missing_fields: tuple[str, ...] = ()
    missing_reasons: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        _require_code(self.code)
        if self.history_sessions < 0:
            raise ValueError("history_sessions cannot be negative")
        if self.security_master_version:
            _require_text(self.security_master_version, "security_master_version")
        if self.history_version:
            _require_text(self.history_version, "history_version")
        risk_versions = _freeze_optional_versions(self.risk_component_versions, "risk component versions")
        normalized_values = dict(sorted(self.values.items()))
        for name, value in normalized_values.items():
            _require_text(name, "daily feature name")
            _require_finite(value, f"daily feature {name}")
        normalized_missing = _sorted_unique_text(self.missing_fields, "missing_fields")
        normalized_reasons = dict(sorted(self.missing_reasons.items()))
        if any(not key.strip() or not value.strip() for key, value in normalized_reasons.items()):
            raise ValueError("missing reasons must contain non-empty keys and values")
        field_values = _freeze_field_values(self.field_values, "daily feature")
        _validate_projected_field_values(normalized_values, normalized_missing, normalized_reasons, field_values)
        object.__setattr__(self, "values", MappingProxyType(normalized_values))
        object.__setattr__(self, "field_values", field_values)
        object.__setattr__(self, "risk_component_versions", risk_versions)
        object.__setattr__(self, "missing_fields", normalized_missing)
        object.__setattr__(self, "missing_reasons", MappingProxyType(normalized_reasons))

    @property
    def has_security_master(self) -> bool:
        return bool(self.security_master_version)

    @property
    def has_core_history(self) -> bool:
        return bool(self.history_version) and self.history_sessions >= CORE_HISTORY_MIN_SESSIONS


@dataclass(frozen=True)
class DailyFeaturePack:
    trade_date: date
    sequence: int
    observed_at: datetime
    received_at: datetime
    config_version: str
    calendar_version: str
    rows: tuple[DailyFeatureRow, ...]
    source_versions: Mapping[str, str]
    coverage: DataPlaneCoverage
    schema_version: str = DAILY_FEATURE_PACK_SCHEMA_VERSION
    content_hash: str = field(init=False)
    version: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_epoch_coordinates(self, DAILY_FEATURE_PACK_SCHEMA_VERSION)
        _require_text(self.calendar_version, "calendar_version")
        rows = tuple(sorted(self.rows, key=lambda row: row.code))
        if not rows:
            raise ValueError("daily feature rows must not be empty")
        _require_unique_codes(tuple(row.code for row in rows), "daily feature rows")
        if any(row.data_as_of >= self.trade_date for row in rows):
            raise ValueError("daily feature data_as_of must precede trade_date")
        row_codes = {row.code for row in rows}
        if not set(self.coverage.potential_executable_codes) <= row_codes:
            raise ValueError("potential executable codes must exist in daily feature rows")
        if not set(self.coverage.candidate_codes) <= row_codes:
            raise ValueError("candidate coverage codes must exist in daily feature rows")
        rows_by_code = {row.code: row for row in rows}
        if any(not rows_by_code[code].has_security_master for code in self.coverage.security_master_codes):
            raise ValueError("security-master coverage requires a versioned master fact")
        if any(not rows_by_code[code].has_core_history for code in self.coverage.candidate_history_codes):
            raise ValueError("candidate history coverage requires a versioned fact with at least 20 sessions")
        for row in rows:
            _validate_epoch_field_times(row.field_values, self.observed_at, self.received_at)
        sources = _freeze_source_versions(self.source_versions)
        payload_hash = _content_hash(
            {
                "schema_version": self.schema_version,
                "trade_date": self.trade_date,
                "sequence": self.sequence,
                "observed_at": self.observed_at,
                "received_at": self.received_at,
                "config_version": self.config_version,
                "calendar_version": self.calendar_version,
                "rows": rows,
                "source_versions": sources,
                "coverage": self.coverage,
            }
        )
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "source_versions", sources)
        object.__setattr__(self, "content_hash", payload_hash)
        object.__setattr__(self, "version", _version("daily", self.trade_date, self.sequence, payload_hash))


@dataclass(frozen=True)
class MarketEpoch:
    trade_date: date
    sequence: int
    observed_at: datetime
    received_at: datetime
    config_version: str
    daily_feature_pack_version: str
    quotes: tuple[MarketQuote, ...]
    source_versions: Mapping[str, str]
    field_values: Mapping[str, Mapping[str, FieldValue]]
    market_regime: Literal["risk_on", "neutral", "risk_off"] = "neutral"
    degraded_reasons: tuple[str, ...] = ()
    schema_version: str = MARKET_EPOCH_SCHEMA_VERSION
    content_hash: str = field(init=False)
    version: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_epoch_coordinates(self, MARKET_EPOCH_SCHEMA_VERSION)
        _require_text(self.daily_feature_pack_version, "daily_feature_pack_version")
        quotes = tuple(sorted(self.quotes, key=lambda quote: quote.code))
        if not quotes:
            raise ValueError("market quotes must not be empty")
        _require_unique_codes(tuple(quote.code for quote in quotes), "market quotes")
        for quote in quotes:
            _validate_market_quote(quote, self.observed_at, self.received_at)
        field_values = _freeze_nested_field_values(self.field_values, "market")
        _validate_epoch_codes(field_values, tuple(quote.code for quote in quotes), "market field lineage")
        quotes_by_code = {quote.code: quote for quote in quotes}
        for code, values in field_values.items():
            _require_lineage_fields(values, _MARKET_REQUIRED_LINEAGE_FIELDS, f"market field lineage for {code}")
            _validate_object_field_values(quotes_by_code[code], values, _MARKET_REQUIRED_LINEAGE_FIELDS)
            _validate_epoch_field_times(values, self.observed_at, self.received_at)
        if self.market_regime not in {"risk_on", "neutral", "risk_off"}:
            raise ValueError("market epoch market_regime is invalid")
        sources = _freeze_source_versions(self.source_versions)
        degraded = _sorted_unique_reason_codes(self.degraded_reasons, "degraded_reasons")
        payload_hash = _content_hash(
            {
                "schema_version": self.schema_version,
                "trade_date": self.trade_date,
                "sequence": self.sequence,
                "observed_at": self.observed_at,
                "received_at": self.received_at,
                "config_version": self.config_version,
                "daily_feature_pack_version": self.daily_feature_pack_version,
                "quotes": quotes,
                "source_versions": sources,
                "field_values": field_values,
                "market_regime": self.market_regime,
                "degraded_reasons": degraded,
            }
        )
        object.__setattr__(self, "quotes", quotes)
        object.__setattr__(self, "source_versions", sources)
        object.__setattr__(self, "field_values", field_values)
        object.__setattr__(self, "degraded_reasons", degraded)
        object.__setattr__(self, "content_hash", payload_hash)
        object.__setattr__(self, "version", _version("market", self.trade_date, self.sequence, payload_hash))


@dataclass(frozen=True)
class CandidateFeatureRow:
    code: str
    values: Mapping[str, float | None]
    field_values: Mapping[str, FieldValue]
    missing_fields: tuple[str, ...] = ()
    missing_reasons: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        _require_code(self.code)
        normalized_values = dict(sorted(self.values.items()))
        declared_fields = set(normalized_values).union(self.missing_fields, self.missing_reasons)
        unsupported = sorted(declared_fields.difference(CANDIDATE_REALTIME_FEATURES))
        if unsupported:
            raise ValueError(f"candidate feature rows contain unsupported realtime fields: {','.join(unsupported)}")
        for name, value in normalized_values.items():
            _require_text(name, "candidate feature name")
            _require_finite(value, f"candidate feature {name}")
        normalized_missing = _sorted_unique_text(self.missing_fields, "candidate missing_fields")
        normalized_reasons = dict(sorted(self.missing_reasons.items()))
        if any(not key.strip() or not value.strip() for key, value in normalized_reasons.items()):
            raise ValueError("candidate missing reasons must contain non-empty keys and values")
        field_values = _freeze_field_values(self.field_values, "candidate feature")
        _validate_projected_field_values(normalized_values, normalized_missing, normalized_reasons, field_values)
        object.__setattr__(self, "values", MappingProxyType(normalized_values))
        object.__setattr__(self, "field_values", field_values)
        object.__setattr__(self, "missing_fields", normalized_missing)
        object.__setattr__(self, "missing_reasons", MappingProxyType(normalized_reasons))


@dataclass(frozen=True)
class CandidateQuoteEpoch:
    trade_date: date
    sequence: int
    observed_at: datetime
    received_at: datetime
    config_version: str
    market_epoch_version: str
    quotes: tuple[LiveQuote, ...]
    source_versions: Mapping[str, str]
    field_values: Mapping[str, Mapping[str, FieldValue]]
    requested_codes: tuple[str, ...] = ()
    feature_rows: tuple[CandidateFeatureRow, ...] = ()
    degraded_reasons: tuple[str, ...] = ()
    schema_version: str = CANDIDATE_QUOTE_EPOCH_SCHEMA_VERSION
    content_hash: str = field(init=False)
    version: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_epoch_coordinates(self, CANDIDATE_QUOTE_EPOCH_SCHEMA_VERSION)
        _require_text(self.market_epoch_version, "market_epoch_version")
        quotes = tuple(sorted(self.quotes, key=lambda quote: quote.code))
        _require_unique_codes(tuple(quote.code for quote in quotes), "candidate quotes")
        for quote in quotes:
            _validate_live_quote(quote, self.observed_at, self.received_at)
        field_values = _freeze_nested_field_values(self.field_values, "candidate quote")
        _validate_epoch_codes(field_values, tuple(quote.code for quote in quotes), "candidate quote field lineage")
        quotes_by_code = {quote.code: quote for quote in quotes}
        for code, values in field_values.items():
            _require_lineage_fields(
                values,
                _CANDIDATE_REQUIRED_LINEAGE_FIELDS,
                f"candidate quote field lineage for {code}",
            )
            _validate_object_field_values(quotes_by_code[code], values, _CANDIDATE_REQUIRED_LINEAGE_FIELDS)
            _validate_epoch_field_times(values, self.observed_at, self.received_at)
        feature_rows = tuple(sorted(self.feature_rows, key=lambda row: row.code))
        _require_unique_codes(tuple(row.code for row in feature_rows), "candidate feature rows")
        quote_codes = {quote.code for quote in quotes}
        requested_codes = tuple(sorted(self.requested_codes or quote_codes))
        _require_unique_codes(requested_codes, "candidate requested_codes")
        for code in requested_codes:
            _require_code(code)
        if not quote_codes.issubset(requested_codes):
            raise ValueError("candidate quotes must be a subset of requested_codes")
        if any(row.code not in quote_codes for row in feature_rows):
            raise ValueError("candidate feature rows must reference candidate quote codes")
        for row in feature_rows:
            _validate_epoch_field_times(row.field_values, self.observed_at, self.received_at)
        sources = _freeze_source_versions(self.source_versions)
        degraded = _sorted_unique_reason_codes(self.degraded_reasons, "degraded_reasons")
        payload_hash = _content_hash(
            {
                "schema_version": self.schema_version,
                "trade_date": self.trade_date,
                "sequence": self.sequence,
                "observed_at": self.observed_at,
                "received_at": self.received_at,
                "config_version": self.config_version,
                "market_epoch_version": self.market_epoch_version,
                "quotes": quotes,
                "requested_codes": requested_codes,
                "feature_rows": feature_rows,
                "source_versions": sources,
                "field_values": field_values,
                "degraded_reasons": degraded,
            }
        )
        object.__setattr__(self, "quotes", quotes)
        object.__setattr__(self, "requested_codes", requested_codes)
        object.__setattr__(self, "feature_rows", feature_rows)
        object.__setattr__(self, "source_versions", sources)
        object.__setattr__(self, "field_values", field_values)
        object.__setattr__(self, "degraded_reasons", degraded)
        object.__setattr__(self, "content_hash", payload_hash)
        object.__setattr__(self, "version", _version("candidate", self.trade_date, self.sequence, payload_hash))


@dataclass(frozen=True)
class ResearchEpoch:
    trade_date: date
    sequence: int
    observed_at: datetime
    received_at: datetime
    config_version: str
    observations: Mapping[str, ResearchObservation]
    source_versions: Mapping[str, str]
    field_values: Mapping[str, Mapping[str, FieldValue]]
    degraded_reasons: tuple[str, ...] = ()
    schema_version: str = RESEARCH_EPOCH_SCHEMA_VERSION
    content_hash: str = field(init=False)
    version: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_epoch_coordinates(self, RESEARCH_EPOCH_SCHEMA_VERSION)
        observations = dict(sorted(self.observations.items()))
        for code, observation in observations.items():
            _require_code(code)
            _validate_research_observation(observation, self.observed_at, self.received_at)
        field_values = _freeze_nested_field_values(self.field_values, "research")
        _validate_epoch_codes(field_values, tuple(observations), "research field lineage")
        for code, values in field_values.items():
            _require_lineage_fields(values, _RESEARCH_REQUIRED_LINEAGE_FIELDS, f"research field lineage for {code}")
            _validate_epoch_field_times(values, self.observed_at, self.received_at)
        sources = _freeze_source_versions(self.source_versions)
        degraded = _sorted_unique_reason_codes(self.degraded_reasons, "degraded_reasons")
        payload_hash = _content_hash(
            {
                "schema_version": self.schema_version,
                "trade_date": self.trade_date,
                "sequence": self.sequence,
                "observed_at": self.observed_at,
                "received_at": self.received_at,
                "config_version": self.config_version,
                "observations": observations,
                "source_versions": sources,
                "field_values": field_values,
                "degraded_reasons": degraded,
            }
        )
        object.__setattr__(self, "observations", MappingProxyType(observations))
        object.__setattr__(self, "source_versions", sources)
        object.__setattr__(self, "field_values", field_values)
        object.__setattr__(self, "degraded_reasons", degraded)
        object.__setattr__(self, "content_hash", payload_hash)
        object.__setattr__(self, "version", _version("research", self.trade_date, self.sequence, payload_hash))


def _validate_epoch_coordinates(
    epoch: _EpochCoordinates,
    expected_schema: str,
) -> None:
    if epoch.sequence < 0:
        raise ValueError("epoch sequence cannot be negative")
    _require_shanghai_time(epoch.observed_at, "observed_at")
    _require_shanghai_time(epoch.received_at, "received_at")
    if epoch.received_at < epoch.observed_at:
        raise ValueError("received_at cannot precede observed_at")
    if epoch.observed_at.date() != epoch.trade_date:
        raise ValueError("epoch trade_date must match observed_at in Asia/Shanghai")
    _require_text(epoch.config_version, "config_version")
    if epoch.schema_version != expected_schema:
        raise ValueError(f"epoch schema_version must be {expected_schema}")


def _validate_market_quote(quote: MarketQuote, observed_at: datetime, received_at: datetime) -> None:
    _require_code(quote.code)
    _require_shanghai_time(quote.source_time, "market quote source_time")
    _require_shanghai_time(quote.received_time, "market quote received_time")
    if quote.received_time < quote.source_time:
        raise ValueError("market quote received_time cannot precede source_time")
    if quote.source_time > observed_at or quote.received_time > received_at:
        raise ValueError("market quote cannot be from the future")
    for item in fields(quote):
        value = getattr(quote, item.name)
        if isinstance(value, float):
            _require_finite(value, f"market quote {item.name}")


def _validate_live_quote(quote: LiveQuote, observed_at: datetime, received_at: datetime) -> None:
    _require_code(quote.code)
    _require_shanghai_time(quote.source_time, "candidate quote source_time")
    _require_shanghai_time(quote.received_time, "candidate quote received_time")
    if quote.received_time < quote.source_time:
        raise ValueError("candidate quote received_time cannot precede source_time")
    if quote.source_time > observed_at or quote.received_time > received_at:
        raise ValueError("candidate quote cannot be from the future")
    _require_finite(quote.price, "candidate quote price")
    _require_finite(quote.pct_change, "candidate quote pct_change")
    deviation = quote.cross_source_deviation_pct
    if deviation is None or not math.isfinite(deviation) or deviation < 0.0:
        raise ValueError("candidate quote cross-source deviation must be finite and non-negative")
    if not quote.cross_source_verified or deviation > 0.5:
        raise ValueError("candidate quote must be cross-source verified with deviation <= 0.5")


def _validate_research_observation(
    observation: ResearchObservation,
    observed_at: datetime,
    received_at: datetime,
) -> None:
    _sorted_unique_reason_codes(observation.source_errors, "research source_errors")
    _validate_financial_time(observation, observed_at)
    _validate_announcement_times(observation, observed_at)
    _validate_risk_fact_times(observation, observed_at)
    _validate_evidence_times(observation, observed_at, received_at)


def _validate_financial_time(observation: ResearchObservation, observed_at: datetime) -> None:
    if observation.financial is not None:
        _require_shanghai_time(observation.financial.published_at, "financial published_at")
        if observation.financial.published_at > observed_at:
            raise ValueError("research epoch cannot contain future financial data")


def _validate_announcement_times(observation: ResearchObservation, observed_at: datetime) -> None:
    for announcement in observation.announcements:
        _require_shanghai_time(announcement.published_at, "announcement published_at")
        if announcement.published_at > observed_at:
            raise ValueError("research epoch cannot contain future announcements")


def _validate_risk_fact_times(observation: ResearchObservation, observed_at: datetime) -> None:
    for fact in observation.corporate_risk_facts:
        _require_shanghai_time(fact.announced_at, "risk fact announced_at")
        if fact.announced_at > observed_at:
            raise ValueError("research epoch cannot contain future risk facts")
        if fact.resolved_at is not None:
            _require_shanghai_time(fact.resolved_at, "risk fact resolved_at")
            if fact.resolved_at > observed_at:
                raise ValueError("research epoch cannot contain future risk resolutions")


def _validate_evidence_times(
    observation: ResearchObservation,
    observed_at: datetime,
    received_at: datetime,
) -> None:
    for evidence in observation.evidence:
        _require_shanghai_time(evidence.published_at, "evidence published_at")
        if evidence.published_at > observed_at:
            raise ValueError("research epoch cannot contain future evidence")
        if evidence.received_at is not None:
            _require_shanghai_time(evidence.received_at, "evidence received_at")
            if evidence.received_at > received_at:
                raise ValueError("research evidence cannot be received after the epoch")


def _require_shanghai_time(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if getattr(value.tzinfo, "key", None) != _SHANGHAI_TIMEZONE:
        raise ValueError(f"{name} must use Asia/Shanghai")


def _require_code(code: str) -> None:
    if len(code) != 6 or not code.isdigit():
        raise ValueError("stock code must contain exactly six digits")


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_finite(value: float | None, name: str) -> None:
    if value is not None and not math.isfinite(value):
        raise ValueError(f"{name} must be finite when present")


def _require_unique_codes(codes: tuple[str, ...], name: str) -> None:
    if len(codes) != len(set(codes)):
        raise ValueError(f"{name} must contain unique codes")


def _sorted_unique_codes(codes: tuple[str, ...], name: str) -> tuple[str, ...]:
    for code in codes:
        _require_code(code)
    normalized = tuple(sorted(set(codes)))
    if len(normalized) != len(codes):
        raise ValueError(f"{name} must contain unique codes")
    return normalized


def _sorted_unique_text(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if any(not value.strip() for value in values):
        raise ValueError(f"{name} must contain non-empty values")
    return tuple(sorted(set(values)))


def _sorted_unique_reason_codes(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if any(_REASON_CODE.fullmatch(value) is None for value in values):
        raise ValueError(f"{name} must contain structured reason codes")
    return tuple(sorted(set(values)))


def _freeze_source_versions(source_versions: Mapping[str, str]) -> Mapping[str, str]:
    normalized = dict(sorted(source_versions.items()))
    if not normalized:
        raise ValueError("source_versions must not be empty")
    if any(not source.strip() or not version.strip() for source, version in normalized.items()):
        raise ValueError("source versions must contain non-empty sources and versions")
    return MappingProxyType(normalized)


def _freeze_field_values(values: Mapping[str, FieldValue], name: str) -> Mapping[str, FieldValue]:
    normalized = dict(sorted(values.items()))
    if not normalized:
        raise ValueError(f"{name} field lineage must not be empty")
    for field_name, value in normalized.items():
        _require_text(field_name, f"{name} field name")
        if field_name != value.name:
            raise ValueError(f"{name} field lineage key must match FieldValue.name")
    return MappingProxyType(normalized)


def _freeze_nested_field_values(
    values: Mapping[str, Mapping[str, FieldValue]],
    name: str,
) -> Mapping[str, Mapping[str, FieldValue]]:
    normalized: dict[str, Mapping[str, FieldValue]] = {}
    for code, fields_by_name in sorted(values.items()):
        _require_code(code)
        normalized[code] = _freeze_field_values(fields_by_name, name)
    return MappingProxyType(normalized)


def _validate_projected_field_values(
    values: Mapping[str, float | None],
    missing_fields: tuple[str, ...],
    missing_reasons: Mapping[str, str],
    field_values: Mapping[str, FieldValue],
) -> None:
    declared = set(values).union(missing_fields, missing_reasons)
    if not declared <= set(field_values):
        raise ValueError("every projected or missing field must carry field lineage")
    for name, value in values.items():
        if field_values[name].value != value:
            raise ValueError("projected field value must match field lineage value")
    for name in missing_fields:
        field_value = field_values[name]
        if field_value.value is not None or field_value.quality is not FieldQualityState.MISSING:
            raise ValueError("missing projected fields require missing field lineage")


def _validate_epoch_field_times(
    values: Mapping[str, FieldValue],
    observed_at: datetime,
    received_at: datetime,
) -> None:
    for value in values.values():
        _require_shanghai_time(value.source_time, f"{value.name} source_time")
        _require_shanghai_time(value.received_time, f"{value.name} received_time")
        if value.source_time > observed_at or value.received_time > received_at:
            raise ValueError("field lineage cannot be from the future")


def _validate_epoch_codes(
    values: Mapping[str, Mapping[str, FieldValue]],
    codes: tuple[str, ...],
    name: str,
) -> None:
    if set(values) != set(codes):
        raise ValueError(f"{name} must exactly match epoch codes")


def _require_lineage_fields(
    values: Mapping[str, FieldValue],
    required: frozenset[str],
    name: str,
) -> None:
    missing = sorted(required.difference(values))
    if missing:
        raise ValueError(f"{name} is missing required fields: {','.join(missing)}")


def _validate_object_field_values(
    value: object,
    field_values: Mapping[str, FieldValue],
    field_names: frozenset[str],
) -> None:
    for name in field_names:
        projected = _field_scalar(getattr(value, name))
        if field_values[name].value != projected:
            raise ValueError(f"{name} must match field lineage value")


def _field_scalar(value: object) -> str | int | float | bool | None:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported field lineage scalar: {type(value).__name__}")


def _freeze_optional_versions(versions: Mapping[str, str], name: str) -> Mapping[str, str]:
    normalized = dict(sorted(versions.items()))
    if any(not key.strip() or not value.strip() for key, value in normalized.items()):
        raise ValueError(f"{name} must contain non-empty keys and versions")
    return MappingProxyType(normalized)


def _version(prefix: str, trade_date: date, sequence: int, content_hash: str) -> str:
    return f"{prefix}:{trade_date.isoformat()}:{sequence}:{content_hash[:16]}"


def _content_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        _canonicalize(payload),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonicalize(value: object) -> _CanonicalValue:
    result: _CanonicalValue
    if isinstance(value, Enum):
        result = _canonicalize(value.value)
    elif value is None or isinstance(value, (str, int, bool)):
        result = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("epoch content must contain only finite floats")
        result = value
    elif isinstance(value, datetime):
        result = value.isoformat()
    elif isinstance(value, date):
        result = value.isoformat()
    elif isinstance(value, Mapping):
        result = {str(key): _canonicalize(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    elif isinstance(value, (tuple, list)):
        result = [_canonicalize(item) for item in value]
    elif is_dataclass(value) and not isinstance(value, type):
        result = {item.name: _canonicalize(getattr(value, item.name)) for item in fields(value)}
    else:
        raise TypeError(f"unsupported epoch content type: {type(value).__name__}")
    return result


__all__ = [
    "CANDIDATE_QUOTE_EPOCH_SCHEMA_VERSION",
    "CANDIDATE_REALTIME_FEATURES",
    "CORE_HISTORY_MIN_SESSIONS",
    "DAILY_FEATURE_PACK_SCHEMA_VERSION",
    "MARKET_EPOCH_SCHEMA_VERSION",
    "RESEARCH_EPOCH_SCHEMA_VERSION",
    "CandidateFeatureRow",
    "CandidateQuoteEpoch",
    "DataPlaneCoverage",
    "DailyFeaturePack",
    "DailyFeatureRow",
    "MarketEpoch",
    "ResearchEpoch",
]
