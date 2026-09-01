"""Point-in-time H1 archive contracts.

The values in this module are deliberately independent from the production
score and from the older H0 screening identity.  They describe only what can
be proved to have been available at a historical decision anchor.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from trader.domain.research.historical_screening import HistoricalPriceBar

_IDENTITY = re.compile(r"^[a-z0-9_]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
SHANGHAI = ZoneInfo("Asia/Shanghai")
H1_RESEARCH_IDENTITY = "score_h1_point_in_time_v1"
H1_REGISTERED_ON = date(2026, 9, 1)
H1_SOURCE_CUTOFF = date(2026, 8, 31)
H1_MAX_HISTORY_SESSIONS = 1600
H1_MIN_COMMON_DAYS = 1000
H1_MIN_COVERAGE = 0.95
H1_TERMINAL_HOLDOUT_DAYS = 200

H1Strategy = Literal["today", "tomorrow", "d25"]
H1AnchorKind = Literal["today_1120", "tomorrow_1450", "d25_1450"]
H1CoverageState = Literal["coverage_ready", "historical_data_insufficient"]


@dataclass(frozen=True)
class H1PointInTimeSpec:
    strategy: H1Strategy
    research_identity: str = H1_RESEARCH_IDENTITY
    registered_on: date = H1_REGISTERED_ON
    source_cutoff: date = H1_SOURCE_CUTOFF
    max_history_sessions: int = H1_MAX_HISTORY_SESSIONS
    minimum_common_days: int = H1_MIN_COMMON_DAYS
    minimum_coverage_ratio: float = H1_MIN_COVERAGE
    terminal_holdout_days: int = H1_TERMINAL_HOLDOUT_DAYS
    promotion_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.strategy not in ("today", "tomorrow", "d25"):
            raise ValueError("H1 strategy is invalid")
        if self.research_identity != H1_RESEARCH_IDENTITY or _IDENTITY.fullmatch(self.research_identity) is None:
            raise ValueError("H1 identity is invalid")
        if self.registered_on != H1_REGISTERED_ON or self.source_cutoff != H1_SOURCE_CUTOFF:
            raise ValueError("H1 registration and source cutoff are fixed")
        if self.max_history_sessions != H1_MAX_HISTORY_SESSIONS:
            raise ValueError("H1 history bound must be 1600 sessions")
        if self.minimum_common_days < H1_MIN_COMMON_DAYS or self.terminal_holdout_days < H1_TERMINAL_HOLDOUT_DAYS:
            raise ValueError("H1 coverage thresholds are too low")
        if not math.isclose(self.minimum_coverage_ratio, H1_MIN_COVERAGE) or not 0 < self.minimum_coverage_ratio <= 1:
            raise ValueError("H1 coverage threshold is invalid")
        if self.promotion_authority:
            raise ValueError("H1 research cannot have production authority")
        payload = {
            field.name: _canonical(getattr(self, field.name)) for field in dataclasses.fields(self) if field.init
        }
        object.__setattr__(self, "content_hash", _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"))))

    @property
    def anchor_kind(self) -> H1AnchorKind:
        return {"today": "today_1120", "tomorrow": "tomorrow_1450", "d25": "d25_1450"}[self.strategy]  # type: ignore[return-value]

    @property
    def anchor_time(self) -> time:
        return time(11, 20) if self.strategy == "today" else time(14, 50)


@dataclass(frozen=True)
class H1PointInTimeRecord:
    strategy: H1Strategy
    code: str
    trade_date: date
    observed_at: datetime
    daily_bar: HistoricalPriceBar
    anchor_price: float
    anchor_volume: float
    anchor_amount: float
    security_state_hash: str
    sector_hash: str
    risk_facts_hash: str
    tail_field_hash: str = ""
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _validate_h1_record_identity(self)
        _validate_h1_record_anchor(self)
        _validate_h1_record_hashes(self)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _validate_h1_record_identity(record: H1PointInTimeRecord) -> None:
    if record.strategy not in ("today", "tomorrow", "d25") or len(record.code) != 6 or not record.code.isdigit():
        raise ValueError("H1 record identity is invalid")
    if record.daily_bar.trade_date != record.trade_date or record.daily_bar.adjustment != "qfq":
        raise ValueError("H1 record requires a matching qfq daily bar")
    if record.trade_date > H1_SOURCE_CUTOFF:
        raise ValueError("H1 record exceeds source cutoff")


def _validate_h1_record_anchor(record: H1PointInTimeRecord) -> None:
    if record.observed_at.tzinfo is None or record.observed_at.utcoffset() is None:
        raise ValueError("H1 observation must be timezone-aware")
    observed = record.observed_at.astimezone(SHANGHAI)
    if observed.date() != record.trade_date:
        raise ValueError("H1 observation date must match trade date")
    expected = time(11, 20) if record.strategy == "today" else time(14, 50)
    if observed.timetz().replace(tzinfo=None) != expected:
        raise ValueError("H1 observation must match the exact strategy anchor")
    if not math.isfinite(record.anchor_price) or record.anchor_price <= 0:
        raise ValueError("H1 anchor price is invalid")
    if any(not math.isfinite(value) or value < 0 for value in (record.anchor_volume, record.anchor_amount)):
        raise ValueError("H1 anchor flow is invalid")


def _validate_h1_record_hashes(record: H1PointInTimeRecord) -> None:
    for value in (record.security_state_hash, record.sector_hash, record.risk_facts_hash):
        if _SHA256.fullmatch(value) is None:
            raise ValueError("H1 point-in-time fact identity is invalid")
    if record.tail_field_hash and _SHA256.fullmatch(record.tail_field_hash) is None:
        raise ValueError("H1 tail identity is invalid")


@dataclass(frozen=True)
class H1CapabilityProbe:
    source: str
    earliest_available: date | None
    supports_today_1120: bool
    supports_1450: bool
    adjustment_semantics: str
    security_state_effective_at: bool
    page_size: int
    estimated_requests: int
    estimated_bytes: int
    estimated_seconds: float
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if not self.source or self.adjustment_semantics not in ("qfq", "unsupported"):
            raise ValueError("H1 capability source semantics are invalid")
        if self.page_size < 1 or self.estimated_requests < 0 or self.estimated_bytes < 0:
            raise ValueError("H1 capability limits are invalid")
        if not math.isfinite(self.estimated_seconds) or self.estimated_seconds < 0:
            raise ValueError("H1 capability estimate is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def point_in_time_anchors_proven(self) -> bool:
        return self.supports_today_1120 and self.supports_1450 and self.security_state_effective_at


@dataclass(frozen=True)
class H1CoverageManifest:
    spec_hash: str
    universe_hash: str
    histories_hash: str
    calendar_hash: str
    field_coverage_hash: str
    source_responses_hash: str
    completed_codes: int
    universe_count: int
    common_trade_days: int
    terminal_holdout_days: int
    state: H1CoverageState
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        for value in (
            self.spec_hash,
            self.universe_hash,
            self.histories_hash,
            self.calendar_hash,
            self.field_coverage_hash,
            self.source_responses_hash,
        ):
            if _SHA256.fullmatch(value) is None:
                raise ValueError("H1 manifest hash is invalid")
        if self.universe_count < 0 or self.completed_codes < 0 or self.completed_codes > self.universe_count:
            raise ValueError("H1 manifest counts are invalid")
        if self.common_trade_days < 0 or self.terminal_holdout_days < 0:
            raise ValueError("H1 manifest dates are invalid")
        if self.state not in ("coverage_ready", "historical_data_insufficient"):
            raise ValueError("H1 coverage state is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class H1CoverageAudit:
    strategy: H1Strategy
    manifest: H1CoverageManifest
    coverage_ratio: float
    terminal_holdout_opened: bool = False

    def __post_init__(self) -> None:
        if self.strategy not in ("today", "tomorrow", "d25") or not 0 <= self.coverage_ratio <= 1:
            raise ValueError("H1 audit values are invalid")
        if self.terminal_holdout_opened:
            raise ValueError("H1 coverage audit cannot open terminal holdout")


def canonical_hash(value: object) -> str:
    return _sha256(json.dumps(_canonical(value), ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def _canonical(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return {field.name: _canonical(getattr(value, field.name)) for field in dataclasses.fields(value) if field.init}
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    return value


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "H1CapabilityProbe",
    "H1CoverageAudit",
    "H1CoverageManifest",
    "H1PointInTimeRecord",
    "H1PointInTimeSpec",
    "H1AnchorKind",
    "H1CoverageState",
    "H1Strategy",
    "H1_RESEARCH_IDENTITY",
    "H1_REGISTERED_ON",
    "H1_SOURCE_CUTOFF",
    "canonical_hash",
]
