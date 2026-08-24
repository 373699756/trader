"""Typed boundaries used by the independent V2 scheduler runtime."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from trader.application.ports.market import ResearchRefreshResult
from trader.application.research_audit import V2CommittedResearchAudit
from trader.application.schedule import MarketPhase
from trader.application.shutdown import ShutdownDeadline, ShutdownStep
from trader.domain.recommendation.decision_identity import DecisionIdentity, DecisionOverlay, ScoredDecision
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


@dataclass(frozen=True)
class V2ResearchIntent:
    strategy: Strategy
    trade_date: date
    priority_codes: tuple[str, ...]
    candidate_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.strategy not in {Strategy.TODAY, Strategy.TOMORROW, Strategy.D25}:
            raise ValueError("research intent requires a scored strategy")
        priority = _normalize_codes(self.priority_codes)
        candidates = _normalize_codes(self.candidate_codes)
        if not set(priority).issubset(candidates):
            raise ValueError("research priority codes must belong to the candidate set")
        object.__setattr__(self, "priority_codes", priority)
        object.__setattr__(self, "candidate_codes", candidates)


@dataclass(frozen=True)
class V2ResearchRuntimeStatus:
    state: str = "stopped"
    running_codes: int = 0
    pending_codes: int = 0
    completed_batches: int = 0
    partial_batches: int = 0
    failed_batches: int = 0
    deferred_codes: int = 0
    cooldown_codes: int = 0
    retry_wait_codes: int = 0
    next_retry_seconds: float = 0.0
    gated_offer_codes: int = 0
    short_circuited_batches: int = 0
    short_circuited_codes: int = 0
    tracked_code_gates: int = 0
    evicted_code_gates: int = 0
    last_error: str = ""
    batch_size: int = 4
    batch_budget_seconds: float = 40.0
    success_cooldown_seconds: float = 60.0
    retry_delays_seconds: tuple[float, ...] = (60.0, 120.0, 240.0, 480.0, 900.0)
    trade_date: str | None = None
    tracked_strategies: int = 0
    tracked_output_codes: int = 0
    next_periodic_at: str | None = None
    intent_offer_count: int = 0
    periodic_offer_count: int = 0
    result_count: int = 0
    rescore_result_count: int = 0


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
    def has_local_draft(self, strategy: Strategy, trade_date: date) -> bool: ...

    def build_local(self, request: V2CycleRequest) -> DecisionIdentity | None: ...

    def initial_overlay(self, decision: ScoredDecision) -> DecisionOverlay: ...

    def research_audit(self, version: str) -> V2CommittedResearchAudit | None: ...

    def research_intent(self, decision: ScoredDecision) -> V2ResearchIntent: ...


class V2ResearchRuntimePort(Protocol):
    def start(self) -> bool: ...

    def stop(
        self,
        *,
        wait: bool,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep: ...

    def observe(self, intent: V2ResearchIntent, request: V2CycleRequest) -> bool: ...

    def offer_due(self, at: datetime, phase: MarketPhase, *, is_trading_day: bool) -> bool: ...

    def wait_until_idle(self, timeout_seconds: float) -> bool: ...

    def status(self) -> V2ResearchRuntimeStatus: ...


class V2ResearchRuntimeFactoryPort(Protocol):
    def __call__(
        self,
        on_result: Callable[[ResearchRefreshResult, bool], None],
    ) -> V2ResearchRuntimePort: ...


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

    def freeze_close_fallback(
        self,
        strategy: Strategy,
        at: datetime,
        current: ScoredDecision,
        *,
        recovery_path: Literal["current", "close_rebuild"],
        official_close_version: str,
    ) -> None: ...


class V2SettlementPort(Protocol):
    def settle(self, at: datetime) -> None: ...


def _require_shanghai(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None or getattr(value.tzinfo, "key", None) != _SHANGHAI.key:
        raise ValueError(f"{label} must use Asia/Shanghai")


def _normalize_codes(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(values))
    if any(len(code) != 6 or not code.isdigit() for code in normalized):
        raise ValueError("research intent codes must be six digits")
    return normalized


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
    "V2ResearchIntent",
    "V2ResearchRuntimeFactoryPort",
    "V2ResearchRuntimePort",
    "V2ResearchRuntimeStatus",
    "V2SettlementPort",
    "V2SettlementUnavailableError",
    "V2TradingCalendarPort",
]
