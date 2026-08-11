"""Typed boundaries used by the independent V2 scheduler runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from trader.domain.recommendation.decision_identity import DecisionIdentity, ScoredDecision
from trader.domain.recommendation.models import Strategy

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_IDENTITY = re.compile(r"^[a-zA-Z0-9_.:-]{1,200}$")


@dataclass(frozen=True)
class SharedDeepSeekRuntimeContract:
    daily_physical_limit: int
    shared_cache: bool
    shared_single_flight: bool

    def __post_init__(self) -> None:
        if self.daily_physical_limit != 168:
            raise ValueError("V2 DeepSeek daily physical limit must remain 168")
        if not self.shared_cache or not self.shared_single_flight:
            raise ValueError("V2 DeepSeek cache and single-flight must be shared")


@dataclass(frozen=True)
class V2CycleRequest:
    strategy: Strategy
    trade_date: date
    observed_at: datetime
    phase: str
    sequence: int
    input_version: str
    allow_review: bool
    review_deadline: datetime

    def __post_init__(self) -> None:
        _require_shanghai(self.observed_at, "cycle observed_at")
        _require_shanghai(self.review_deadline, "cycle review_deadline")
        if self.observed_at.date() != self.trade_date:
            raise ValueError("cycle observation must match its trade date")
        if self.sequence < 1:
            raise ValueError("cycle sequence must be positive")
        if _IDENTITY.fullmatch(self.phase) is None or _IDENTITY.fullmatch(self.input_version) is None:
            raise ValueError("cycle phase and input version must be stable identities")


class V2DataRefreshUnavailableError(RuntimeError):
    """A refresh failed while the last valid V2 data plane remained readable."""


class V2DecisionUnavailableError(RuntimeError):
    """A local decision could not be produced from the retained data plane."""


class V2ReviewUnavailableError(RuntimeError):
    """The shared DeepSeek path failed and the local decision must remain current."""


class V2FreezeUnavailableError(RuntimeError):
    """A freeze attempt failed without changing an existing formal record."""


class V2SettlementUnavailableError(RuntimeError):
    """Background settlement failed without changing current decisions."""


class V2TradingCalendarPort(Protocol):
    def is_trading_day(self, day: date) -> bool: ...


class V2DataRefreshPort(Protocol):
    def refresh(self, request: V2CycleRequest) -> None: ...


class V2DecisionBuilderPort(Protocol):
    def build_local(self, request: V2CycleRequest) -> DecisionIdentity | None: ...


class V2DeepSeekUpgradePort(Protocol):
    @property
    def runtime_contract(self) -> SharedDeepSeekRuntimeContract: ...

    def build_hybrid(self, local: ScoredDecision, request: V2CycleRequest) -> ScoredDecision | None: ...


class V2FreezePort(Protocol):
    def freeze(
        self,
        strategy: Strategy,
        at: datetime,
        current: DecisionIdentity | None,
    ) -> None: ...


class V2SettlementPort(Protocol):
    def settle(self, at: datetime) -> None: ...


def _require_shanghai(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None or getattr(value.tzinfo, "key", None) != _SHANGHAI.key:
        raise ValueError(f"{label} must use Asia/Shanghai")


__all__ = [
    "SharedDeepSeekRuntimeContract",
    "V2CycleRequest",
    "V2DataRefreshPort",
    "V2DataRefreshUnavailableError",
    "V2DecisionBuilderPort",
    "V2DecisionUnavailableError",
    "V2DeepSeekUpgradePort",
    "V2FreezePort",
    "V2FreezeUnavailableError",
    "V2ReviewUnavailableError",
    "V2SettlementPort",
    "V2SettlementUnavailableError",
    "V2TradingCalendarPort",
]
