"""Shanghai-time trading phases and scheduling decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


class MarketPhase(str, Enum):
    CLOSED = "closed"
    WARMUP = "warmup"
    TODAY_OBSERVE = "today_observe"
    TODAY_MAIN = "today_main"
    TODAY_LATE = "today_late"
    MIDDAY = "midday"
    AFTERNOON = "afternoon"
    FINAL_REVIEW = "final_review"
    DEEPSEEK_CUTOFF = "deepseek_cutoff"
    FINAL_QUOTE = "final_quote"
    FROZEN = "frozen"
    AFTER_CLOSE = "after_close"


class SchedulePoint(str, Enum):
    TODAY_FREEZE = "today_freeze"
    DEEPSEEK_CUTOFF = "deepseek_cutoff"
    FINAL_CANDIDATE_QUOTES = "final_candidate_quotes"
    AFTERNOON_FREEZE = "afternoon_freeze"
    CLOSE_QUOTES = "close_quotes"


_PHASE_BOUNDARIES = (
    time(9, 15),
    time(9, 30),
    time(9, 36),
    time(10, 30),
    time(11, 20),
    time(13, 0),
    time(14, 20),
    time(14, 48),
    time(14, 49, 50),
    time(14, 50),
    time(15, 0),
)


@dataclass(frozen=True)
class ScheduleDecision:
    phase: MarketPhase
    should_refresh_market: bool
    should_score: bool
    should_review: bool
    freeze_strategies: tuple[str, ...]


def shanghai_now(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("business clock must be timezone-aware")
    return value.astimezone(SHANGHAI)


def phase_at(value: datetime, *, is_trading_day: bool) -> MarketPhase:
    if not is_trading_day:
        return MarketPhase.CLOSED
    current = shanghai_now(value).time().replace(tzinfo=None)
    if time(9, 15) <= current < time(9, 30):
        return MarketPhase.WARMUP
    if time(9, 30) <= current < time(9, 36):
        return MarketPhase.TODAY_OBSERVE
    if time(9, 36) <= current < time(10, 30):
        return MarketPhase.TODAY_MAIN
    if time(10, 30) <= current < time(11, 20):
        return MarketPhase.TODAY_LATE
    if time(11, 20) <= current < time(13, 0):
        return MarketPhase.MIDDAY
    if time(13, 0) <= current < time(14, 20):
        return MarketPhase.AFTERNOON
    if time(14, 20) <= current < time(14, 48):
        return MarketPhase.FINAL_REVIEW
    if time(14, 48) <= current < time(14, 49, 50):
        return MarketPhase.DEEPSEEK_CUTOFF
    if time(14, 49, 50) <= current < time(14, 50):
        return MarketPhase.FINAL_QUOTE
    if time(14, 50) <= current < time(15, 0):
        return MarketPhase.FROZEN
    if current >= time(15, 0):
        return MarketPhase.AFTER_CLOSE
    return MarketPhase.CLOSED


def decision_at(value: datetime, *, is_trading_day: bool) -> ScheduleDecision:
    phase = phase_at(value, is_trading_day=is_trading_day)
    return ScheduleDecision(
        phase=phase,
        should_refresh_market=phase
        in {
            MarketPhase.WARMUP,
            MarketPhase.TODAY_OBSERVE,
            MarketPhase.TODAY_MAIN,
            MarketPhase.TODAY_LATE,
            MarketPhase.AFTERNOON,
            MarketPhase.FINAL_REVIEW,
            MarketPhase.FINAL_QUOTE,
        },
        should_score=phase
        in {
            MarketPhase.TODAY_OBSERVE,
            MarketPhase.TODAY_MAIN,
            MarketPhase.TODAY_LATE,
            MarketPhase.AFTERNOON,
            MarketPhase.FINAL_REVIEW,
            MarketPhase.FINAL_QUOTE,
        },
        should_review=phase
        in {
            MarketPhase.WARMUP,
            MarketPhase.TODAY_OBSERVE,
            MarketPhase.TODAY_MAIN,
            MarketPhase.TODAY_LATE,
            MarketPhase.AFTERNOON,
            MarketPhase.FINAL_REVIEW,
        },
        freeze_strategies=_freeze_at(value, is_trading_day=is_trading_day),
    )


def trade_date_at(value: datetime) -> date:
    return shanghai_now(value).date()


def freeze_due_at(value: datetime, *, is_trading_day: bool) -> tuple[str, ...]:
    if not is_trading_day:
        return ()
    current = shanghai_now(value).time().replace(tzinfo=None)
    if current >= time(14, 50):
        return ("today", "tomorrow", "d25")
    if current >= time(11, 20):
        return ("today",)
    return ()


def schedule_point_at(value: datetime, *, is_trading_day: bool) -> SchedulePoint | None:
    if not is_trading_day:
        return None
    current = shanghai_now(value).time().replace(tzinfo=None)
    points = {
        time(11, 20): SchedulePoint.TODAY_FREEZE,
        time(14, 48): SchedulePoint.DEEPSEEK_CUTOFF,
        time(14, 49, 50): SchedulePoint.FINAL_CANDIDATE_QUOTES,
        time(14, 50): SchedulePoint.AFTERNOON_FREEZE,
        time(15, 0): SchedulePoint.CLOSE_QUOTES,
    }
    return points.get(current.replace(microsecond=0))


def seconds_until_next_schedule_boundary(value: datetime, *, maximum_seconds: float) -> float:
    local = shanghai_now(value)
    upcoming = (
        local.replace(
            hour=boundary.hour,
            minute=boundary.minute,
            second=boundary.second,
            microsecond=0,
        )
        for boundary in _PHASE_BOUNDARIES
    )
    delays = tuple((boundary - local).total_seconds() for boundary in upcoming if boundary > local)
    if not delays:
        return maximum_seconds
    return max(0.05, min(maximum_seconds, min(delays)))


def _freeze_at(value: datetime, *, is_trading_day: bool) -> tuple[str, ...]:
    point = schedule_point_at(value, is_trading_day=is_trading_day)
    if point is SchedulePoint.TODAY_FREEZE:
        return ("today",)
    if point is SchedulePoint.AFTERNOON_FREEZE:
        return ("tomorrow", "d25")
    return ()


__all__ = [
    "MarketPhase",
    "SHANGHAI",
    "SchedulePoint",
    "ScheduleDecision",
    "decision_at",
    "freeze_due_at",
    "phase_at",
    "schedule_point_at",
    "seconds_until_next_schedule_boundary",
    "shanghai_now",
    "trade_date_at",
]
