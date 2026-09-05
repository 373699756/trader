"""Typed boundaries used by the independent scheduler runtime."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from trader.application.ports.market import ResearchRefreshResult
from trader.application.ports.runtime_status import InputQualityStatus
from trader.application.research.research_audit import CommittedResearchAudit
from trader.application.runtime.cadence import PipelineTask
from trader.application.runtime.schedule import MarketPhase
from trader.application.runtime.shutdown import ShutdownDeadline, ShutdownStep
from trader.domain.recommendation.decision_identity import DecisionIdentity, DecisionOverlay, ScoredDecision
from trader.domain.recommendation.models import Strategy

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_IDENTITY = re.compile(r"^[a-zA-Z0-9_.:-]{1,200}$")

OverlayPublisher = Callable[[DecisionOverlay], object]


@dataclass(frozen=True)
class SharedDeepSeekRuntimeContract:
    daily_physical_limit: int
    shared_cache: bool
    shared_single_flight: bool

    def __post_init__(self) -> None:
        if self.daily_physical_limit != 168:
            raise ValueError("DeepSeek daily physical limit must remain 168")
        if not self.shared_cache or not self.shared_single_flight:
            raise ValueError("DeepSeek cache and single-flight must be shared")


@dataclass(frozen=True)
class CycleRequest:
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
class PipelineTaskRequest:
    task: PipelineTask
    observed_at: datetime
    selected_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_shanghai(self.observed_at, "pipeline task observed_at")
        object.__setattr__(self, "selected_codes", _normalize_codes(self.selected_codes))


@dataclass(frozen=True)
class RefreshOutcome:
    task: PipelineTask
    changed: bool
    data_version: str
    changed_codes: tuple[str, ...]
    completed_at: datetime
    used_fallback: bool

    def __post_init__(self) -> None:
        normalized_version = self.data_version.strip()
        if _IDENTITY.fullmatch(normalized_version) is None:
            raise ValueError("refresh data version must be a stable identity")
        _require_shanghai(self.completed_at, "refresh completed_at")
        normalized_codes = _normalize_codes(self.changed_codes)
        if normalized_codes != self.changed_codes:
            raise ValueError("refresh changed codes must be unique normalized codes")
        if not self.changed and normalized_codes:
            raise ValueError("unchanged refresh cannot declare changed codes")
        object.__setattr__(self, "data_version", normalized_version)


@dataclass(frozen=True)
class ResearchIntent:
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
class ResearchRuntimeStatus:
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


class DataRefreshUnavailableError(RuntimeError):
    """A refresh failed while the last valid data plane remained readable."""


class DecisionUnavailableError(RuntimeError):
    """A local decision could not be produced from the retained data plane."""


class ReviewUnavailableError(RuntimeError):
    """The shared DeepSeek path failed and the local decision must remain current."""


class FreezeUnavailableError(RuntimeError):
    """A freeze attempt failed without changing an existing formal record."""


class SettlementUnavailableError(RuntimeError):
    """Background settlement failed without changing current decisions."""


class TradingCalendarPort(Protocol):
    def is_trading_day(self, day: date) -> bool: ...


class DataRefreshPort(Protocol):
    def refresh_task(self, request: PipelineTaskRequest) -> RefreshOutcome: ...

    def refresh(self, request: CycleRequest) -> None: ...


class DecisionBuilderPort(Protocol):
    def input_quality_status(self) -> tuple[InputQualityStatus, ...]: ...

    def has_local_draft(self, strategy: Strategy, trade_date: date) -> bool: ...

    def build_local(self, request: CycleRequest) -> DecisionIdentity | None: ...

    def initial_overlay(self, decision: ScoredDecision) -> DecisionOverlay: ...

    def refreshed_overlay(
        self,
        decision: ScoredDecision,
        request: CycleRequest,
        previous: DecisionOverlay | None,
    ) -> DecisionOverlay | None: ...

    def research_audit(self, version: str) -> CommittedResearchAudit | None: ...

    def research_intent(self, decision: ScoredDecision) -> ResearchIntent: ...


class ResearchRuntimePort(Protocol):
    def start(self) -> bool: ...

    def stop(
        self,
        *,
        wait: bool,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep: ...

    def observe(self, intent: ResearchIntent, request: CycleRequest) -> bool: ...

    def offer_due(self, at: datetime, phase: MarketPhase, *, is_trading_day: bool) -> bool: ...

    def wait_until_idle(self, timeout_seconds: float) -> bool: ...

    def status(self) -> ResearchRuntimeStatus: ...


class ResearchRuntimeFactoryPort(Protocol):
    def __call__(
        self,
        on_result: Callable[[ResearchRefreshResult, bool], None],
    ) -> ResearchRuntimePort: ...


class DeepSeekUpgradePort(Protocol):
    @property
    def runtime_contract(self) -> SharedDeepSeekRuntimeContract: ...

    def build_hybrid(self, local: ScoredDecision, request: CycleRequest) -> ScoredDecision | None: ...


class FreezePort(Protocol):
    def capture_checkpoint(self, strategy: Strategy, at: datetime) -> None: ...

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


class SettlementPort(Protocol):
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
    "CycleRequest",
    "DataRefreshPort",
    "DataRefreshUnavailableError",
    "DecisionBuilderPort",
    "DecisionUnavailableError",
    "DeepSeekUpgradePort",
    "FreezePort",
    "FreezeUnavailableError",
    "OverlayPublisher",
    "PipelineTaskRequest",
    "RefreshOutcome",
    "ReviewUnavailableError",
    "ResearchIntent",
    "ResearchRuntimeFactoryPort",
    "ResearchRuntimePort",
    "ResearchRuntimeStatus",
    "SettlementPort",
    "SettlementUnavailableError",
    "TradingCalendarPort",
]
