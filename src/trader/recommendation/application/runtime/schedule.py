"""Shanghai-time trading phases and scheduling decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
FREEZE_TIME = time(15, 0)


class MarketPhase(str, Enum):
    CLOSED = "closed"
    WARMUP = "warmup"
    MORNING_OBSERVE = "morning_observe"
    MORNING_MAIN = "morning_main"
    MORNING_LATE = "morning_late"
    MIDDAY = "midday"
    AFTERNOON = "afternoon"
    FINAL_REVIEW = "final_review"
    DEEPSEEK_CUTOFF = "deepseek_cutoff"
    FINAL_QUOTE = "final_quote"
    FROZEN = "frozen"
    AFTER_CLOSE = "after_close"


class SchedulePoint(str, Enum):
    DEEPSEEK_CUTOFF = "deepseek_cutoff"
    AFTERNOON_CHECKPOINT = "afternoon_checkpoint"
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
    time(14, 46),
    time(14, 48),
    time(14, 49, 20),
    time(14, 49, 50),
    FREEZE_TIME,
)
_TRADING_PHASE_RANGES = (
    (time(9, 15), time(9, 30), MarketPhase.WARMUP),
    (time(9, 30), time(9, 36), MarketPhase.MORNING_OBSERVE),
    (time(9, 36), time(10, 30), MarketPhase.MORNING_MAIN),
    (time(10, 30), time(11, 20), MarketPhase.MORNING_LATE),
    (time(11, 20), time(13, 0), MarketPhase.MIDDAY),
    (time(13, 0), time(14, 20), MarketPhase.AFTERNOON),
    (time(14, 20), time(14, 48), MarketPhase.FINAL_REVIEW),
    (time(14, 48), time(14, 49, 50), MarketPhase.DEEPSEEK_CUTOFF),
    (time(14, 49, 50), FREEZE_TIME, MarketPhase.FINAL_QUOTE),
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
    phase = MarketPhase.AFTER_CLOSE if current >= time(15, 0) else MarketPhase.CLOSED
    for start, end, candidate in _TRADING_PHASE_RANGES:
        if start <= current < end:
            phase = candidate
            break
    return phase


def decision_at(value: datetime, *, is_trading_day: bool) -> ScheduleDecision:
    phase = phase_at(value, is_trading_day=is_trading_day)
    return ScheduleDecision(
        phase=phase,
        should_refresh_market=phase
        in {
            MarketPhase.WARMUP,
            MarketPhase.MORNING_OBSERVE,
            MarketPhase.MORNING_MAIN,
            MarketPhase.MORNING_LATE,
            MarketPhase.AFTERNOON,
            MarketPhase.FINAL_REVIEW,
            MarketPhase.FINAL_QUOTE,
        },
        should_score=phase
        in {
            MarketPhase.MORNING_OBSERVE,
            MarketPhase.MORNING_MAIN,
            MarketPhase.MORNING_LATE,
            MarketPhase.AFTERNOON,
            MarketPhase.FINAL_REVIEW,
            MarketPhase.DEEPSEEK_CUTOFF,
            MarketPhase.FINAL_QUOTE,
        },
        should_review=phase
        in {
            MarketPhase.WARMUP,
            MarketPhase.MORNING_OBSERVE,
            MarketPhase.MORNING_MAIN,
            MarketPhase.MORNING_LATE,
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
    if current >= FREEZE_TIME:
        return ("tomorrow", "d25")
    return ()


def startup_freeze_strategies(value: datetime, *, is_trading_day: bool) -> tuple[str, ...]:
    """Return checkpoint-eligible freezes for initialization before close fallback."""

    if not is_trading_day:
        return ()
    current = shanghai_now(value).time().replace(tzinfo=None)
    if current >= FREEZE_TIME:
        return ("tomorrow", "d25")
    return ()


def schedule_point_at(value: datetime, *, is_trading_day: bool) -> SchedulePoint | None:
    if not is_trading_day:
        return None
    current = shanghai_now(value).time().replace(tzinfo=None)
    points = {
        time(14, 48): SchedulePoint.DEEPSEEK_CUTOFF,
        time(14, 49, 20): SchedulePoint.AFTERNOON_CHECKPOINT,
        time(14, 49, 50): SchedulePoint.FINAL_CANDIDATE_QUOTES,
        FREEZE_TIME: SchedulePoint.AFTERNOON_FREEZE,
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
    "startup_freeze_strategies",
    "trade_date_at",
]
