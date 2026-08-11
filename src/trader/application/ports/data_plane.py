"""Typed ports and immutable records for the V2 v2-data repository."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trader.application.ports.types import JsonObject, freeze_json_object


@dataclass(frozen=True, kw_only=True)
class DataPlaneRecord:
    """Common immutable fields for all V2 data-plane records."""

    code: str
    observed_at: datetime
    source: str
    source_time: datetime
    data_version: str
    payload: JsonObject
    payload_hash: str = ""
    schema_version: str = ""

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("security code must be six digits")
        if not self.source:
            raise ValueError("source cannot be empty")
        if not self.data_version:
            raise ValueError("data version cannot be empty")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.source_time.tzinfo is None or self.source_time.utcoffset() is None:
            raise ValueError("source_time must be timezone-aware")
        if self.observed_at < self.source_time:
            raise ValueError("observed_at cannot be before source_time")
        if self.payload_hash and len(self.payload_hash) != 64:
            raise ValueError("payload_hash must be a sha256 hex string when provided")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be an object")
        if not self.payload:
            raise ValueError("data-plane payload must not be empty")
        # Persisted payload should be immutable at write boundary.
        object.__setattr__(self, "payload", freeze_json_object(self.payload))


@dataclass(frozen=True, kw_only=True)
class SecurityMasterRecord(DataPlaneRecord):
    """Security master identity snapshot for one code."""


@dataclass(frozen=True, kw_only=True)
class HistoricalFeatureRecord(DataPlaneRecord):
    """Historical feature payload for a specific trading date."""

    trade_date: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.trade_date:
            raise ValueError("trade_date must not be empty")


@dataclass(frozen=True, kw_only=True)
class RiskEvidenceRecord(DataPlaneRecord):
    """Per-code risk evidence payload for a specific evidence component."""

    evidence_id: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.evidence_id:
            raise ValueError("evidence_id must not be empty")


@dataclass(frozen=True, kw_only=True)
class SourceCursorRecord:
    """Incremental sync cursor state for an external data source."""

    cursor_name: str
    observed_at: datetime
    source: str
    source_time: datetime
    data_version: str
    payload: JsonObject
    payload_hash: str = ""
    schema_version: str = ""
    cursor_value: str

    def __post_init__(self) -> None:
        if not self.cursor_name:
            raise ValueError("cursor_name must not be empty")
        if not self.source:
            raise ValueError("source cannot be empty")
        if not self.data_version:
            raise ValueError("data version cannot be empty")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.source_time.tzinfo is None or self.source_time.utcoffset() is None:
            raise ValueError("source_time must be timezone-aware")
        if self.observed_at < self.source_time:
            raise ValueError("observed_at cannot be before source_time")
        if self.payload_hash and len(self.payload_hash) != 64:
            raise ValueError("payload_hash must be a sha256 hex string when provided")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be an object")
        # Persisted payload should be immutable at write boundary.
        object.__setattr__(self, "payload", freeze_json_object(self.payload))
        if not self.cursor_value:
            raise ValueError("cursor_value must not be empty")


@dataclass(frozen=True, kw_only=True)
class TradingCalendarRecord:
    """Versioned A-share trading-session calendar snapshot."""

    calendar_name: str
    observed_at: datetime
    source: str
    source_time: datetime
    data_version: str
    payload: JsonObject
    payload_hash: str = ""
    schema_version: str = ""

    def __post_init__(self) -> None:
        if not self.calendar_name.strip():
            raise ValueError("calendar_name must not be empty")
        if not self.source.strip() or not self.data_version.strip():
            raise ValueError("calendar source and data version must not be empty")
        for name, value in (("observed_at", self.observed_at), ("source_time", self.source_time)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"calendar {name} must be timezone-aware")
        if self.observed_at < self.source_time:
            raise ValueError("calendar observed_at cannot be before source_time")
        if self.payload_hash and len(self.payload_hash) != 64:
            raise ValueError("calendar payload_hash must be a sha256 hex string when provided")
        if not isinstance(self.payload, dict) or not self.payload:
            raise ValueError("calendar payload must be a non-empty object")
        object.__setattr__(self, "payload", freeze_json_object(self.payload))


class SecurityMasterRepositoryPort(Protocol):
    def save_recent(self, record: SecurityMasterRecord) -> None: ...

    def load_recent(self, code: str) -> SecurityMasterRecord | None: ...

    def save_formal(self, freeze_id: str, record: SecurityMasterRecord) -> None: ...

    def load_formal(self, freeze_id: str, code: str) -> SecurityMasterRecord | None: ...


class HistoricalFeatureRepositoryPort(Protocol):
    def save_recent(self, record: HistoricalFeatureRecord) -> None: ...

    def load_recent(self, code: str, trade_date: str) -> HistoricalFeatureRecord | None: ...

    def save_formal(self, freeze_id: str, record: HistoricalFeatureRecord) -> None: ...

    def load_formal(
        self,
        freeze_id: str,
        code: str,
        trade_date: str,
    ) -> HistoricalFeatureRecord | None: ...


class RiskEvidenceRepositoryPort(Protocol):
    def save_recent(self, record: RiskEvidenceRecord) -> None: ...

    def load_recent(self, code: str, evidence_id: str) -> RiskEvidenceRecord | None: ...

    def save_formal(self, freeze_id: str, record: RiskEvidenceRecord) -> None: ...

    def load_formal(self, freeze_id: str, code: str, evidence_id: str) -> RiskEvidenceRecord | None: ...


class SourceCursorRepositoryPort(Protocol):
    def save_recent(self, record: SourceCursorRecord) -> None: ...

    def load_recent(self, cursor_name: str) -> SourceCursorRecord | None: ...

    def save_formal(self, freeze_id: str, record: SourceCursorRecord) -> None: ...

    def load_formal(self, freeze_id: str, cursor_name: str) -> SourceCursorRecord | None: ...


class TradingCalendarRepositoryPort(Protocol):
    def save_recent(self, record: TradingCalendarRecord) -> None: ...

    def load_recent(self, calendar_name: str) -> TradingCalendarRecord | None: ...

    def save_formal(self, freeze_id: str, record: TradingCalendarRecord) -> None: ...

    def load_formal(self, freeze_id: str, calendar_name: str) -> TradingCalendarRecord | None: ...


class DataPlaneRepositoryError(RuntimeError):
    """Base failure for V2 data-plane persistence."""


class DataPlaneConflictError(DataPlaneRepositoryError):
    """A formal data-plane identity already exists with different content."""


class DataPlaneUnavailableError(DataPlaneRepositoryError):
    """A repository failure prevented trusted read/write/recovery."""


@dataclass(frozen=True)
class DataPlaneRecoverySummary:
    recovered: int = 0
    quarantined: int = 0
    orphaned: int = 0


class DataPlaneWriterPort(Protocol):
    def initialize(self) -> None: ...

    def save_security_master_recent(self, record: SecurityMasterRecord) -> None: ...

    def save_security_master_formal(self, freeze_id: str, record: SecurityMasterRecord) -> None: ...

    def save_historical_feature_recent(self, record: HistoricalFeatureRecord) -> None: ...

    def save_historical_feature_formal(self, freeze_id: str, record: HistoricalFeatureRecord) -> None: ...

    def save_risk_evidence_recent(self, record: RiskEvidenceRecord) -> None: ...

    def save_risk_evidence_formal(self, freeze_id: str, record: RiskEvidenceRecord) -> None: ...

    def save_source_cursor_recent(self, record: SourceCursorRecord) -> None: ...

    def save_source_cursor_formal(self, freeze_id: str, record: SourceCursorRecord) -> None: ...

    def save_trading_calendar_recent(self, record: TradingCalendarRecord) -> None: ...

    def save_trading_calendar_formal(self, freeze_id: str, record: TradingCalendarRecord) -> None: ...

    def recover(self) -> DataPlaneRecoverySummary: ...


class DataPlaneReaderPort(Protocol):
    def load_security_master_recent(self, code: str) -> SecurityMasterRecord | None: ...

    def load_security_master_recent_records(
        self,
        codes: Sequence[str] | None = None,
    ) -> tuple[SecurityMasterRecord, ...]: ...

    def load_security_master_formal(self, freeze_id: str, code: str) -> SecurityMasterRecord | None: ...

    def load_security_master_formal_records(
        self,
        freeze_id: str,
        codes: Sequence[str] | None = None,
    ) -> tuple[SecurityMasterRecord, ...]: ...

    def load_historical_feature_recent(self, code: str, trade_date: str) -> HistoricalFeatureRecord | None: ...

    def load_historical_feature_recent_records(
        self,
        codes: Sequence[str] | None = None,
    ) -> tuple[HistoricalFeatureRecord, ...]: ...

    def load_historical_feature_formal(
        self, freeze_id: str, code: str, trade_date: str
    ) -> HistoricalFeatureRecord | None: ...

    def load_historical_feature_formal_records(
        self,
        freeze_id: str,
        codes: Sequence[str] | None = None,
    ) -> tuple[HistoricalFeatureRecord, ...]: ...

    def load_risk_evidence_recent(self, code: str, evidence_id: str) -> RiskEvidenceRecord | None: ...

    def load_risk_evidence_recent_records(
        self,
        codes: Sequence[str] | None = None,
    ) -> tuple[RiskEvidenceRecord, ...]: ...

    def load_risk_evidence_formal(self, freeze_id: str, code: str, evidence_id: str) -> RiskEvidenceRecord | None: ...

    def load_risk_evidence_formal_records(
        self,
        freeze_id: str,
        codes: Sequence[str] | None = None,
    ) -> tuple[RiskEvidenceRecord, ...]: ...

    def load_source_cursor_recent(self, cursor_name: str) -> SourceCursorRecord | None: ...

    def load_source_cursor_recent_records(
        self,
        cursor_names: Sequence[str] | None = None,
    ) -> tuple[SourceCursorRecord, ...]: ...

    def load_source_cursor_formal(self, freeze_id: str, cursor_name: str) -> SourceCursorRecord | None: ...

    def load_source_cursor_formal_records(
        self,
        freeze_id: str,
        cursor_names: Sequence[str] | None = None,
    ) -> tuple[SourceCursorRecord, ...]: ...

    def load_trading_calendar_recent(self, calendar_name: str) -> TradingCalendarRecord | None: ...

    def load_trading_calendar_recent_records(
        self,
        calendar_names: Sequence[str] | None = None,
    ) -> tuple[TradingCalendarRecord, ...]: ...

    def load_trading_calendar_formal(
        self,
        freeze_id: str,
        calendar_name: str,
    ) -> TradingCalendarRecord | None: ...

    def load_trading_calendar_formal_records(
        self,
        freeze_id: str,
        calendar_names: Sequence[str] | None = None,
    ) -> tuple[TradingCalendarRecord, ...]: ...


class DataPlanePorts(DataPlaneWriterPort, DataPlaneReaderPort, Protocol):
    """Read/write port pair for the V2 data-plane repository."""


__all__ = [
    "HistoricalFeatureRecord",
    "RiskEvidenceRecord",
    "SecurityMasterRecord",
    "SourceCursorRecord",
    "TradingCalendarRecord",
    "DataPlaneRecord",
    "DataPlanePorts",
    "DataPlaneConflictError",
    "DataPlaneRepositoryError",
    "SecurityMasterRepositoryPort",
    "HistoricalFeatureRepositoryPort",
    "RiskEvidenceRepositoryPort",
    "SourceCursorRepositoryPort",
    "TradingCalendarRepositoryPort",
    "DataPlaneUnavailableError",
    "DataPlaneRecoverySummary",
    "DataPlaneReaderPort",
    "DataPlaneWriterPort",
]
