"""Immutable status objects exposed by the recommendation runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from trader.recommendation.application.ports.read_only_queries import InputQualityStatus
from trader.recommendation.application.ports.runtime import ResearchRuntimeStatus, SharedDeepSeekRuntimeContract
from trader.recommendation.application.runtime.cadence import CadencePlannerStatus
from trader.recommendation.application.runtime.latest_wins import LatestWinsStatus
from trader.recommendation.application.runtime.runtime_issues import RuntimeIssue
from trader.recommendation.application.runtime.schedule import MarketPhase
from trader.recommendation.application.pipeline.freeze_publish.decision_observers import DecisionObserverStatus


@dataclass(frozen=True)
class TradingCalendarRuntimeStatus:
    state: Literal["unknown", "ready", "unavailable"]
    trade_date: date | None
    is_trading_day: bool | None
    consecutive_failure_count: int
    next_retry_at: datetime | None


@dataclass(frozen=True)
class SchedulerRuntimeStatus:
    running: bool
    phase: MarketPhase
    config_version: str
    lanes: tuple[LatestWinsStatus, ...]
    hybrid_lanes: tuple[LatestWinsStatus, ...]
    task_lanes: tuple[LatestWinsStatus, ...]
    cadence: CadencePlannerStatus
    observer: DecisionObserverStatus
    deepseek: SharedDeepSeekRuntimeContract
    company_research: ResearchRuntimeStatus
    control_running: bool
    control_inflight: int
    control_rejected_count: int
    refresh_failure_count: int
    decision_failure_count: int
    review_failure_count: int
    overlay_publish_count: int
    overlay_failure_count: int
    local_publish_count: int
    hybrid_publish_count: int
    publish_rejection_count: int
    observer_rejection_count: int
    freeze_completed_count: int
    freeze_failure_count: int
    settlement_completed_count: int
    settlement_failure_count: int
    last_error_code: str
    strategy_error_codes: tuple[tuple[str, str], ...]
    recent_errors: tuple[RuntimeIssue, ...]
    input_quality: tuple[InputQualityStatus, ...]
    calendar: TradingCalendarRuntimeStatus = TradingCalendarRuntimeStatus(
        state="unknown",
        trade_date=None,
        is_trading_day=None,
        consecutive_failure_count=0,
        next_retry_at=None,
    )


__all__ = ["SchedulerRuntimeStatus", "TradingCalendarRuntimeStatus"]
