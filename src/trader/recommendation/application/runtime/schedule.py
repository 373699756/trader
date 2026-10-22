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
    BEFORE_CLOSE = "before_close"
    AFTER_CLOSE = "after_close"


class SchedulePoint(str, Enum):
    AFTERNOON_FREEZE = "afternoon_freeze"
    CLOSE_QUOTES = "close_quotes"


# Extra wakeups serve independent minute/industry collection, not recommendation phases.
_PHASE_BOUNDARIES = (
    time(9, 15),
    time(9, 30),
    time(11, 20),
    time(13, 0),
    time(14, 20),
    time(14, 48),
    FREEZE_TIME,
)


@dataclass(frozen=True)
class ScheduleDecision:
    phase: MarketPhase
    should_refresh_market: bool
    should_score: bool
    should_review: bool
    freeze_strategies: tuple[str, ...]


def ShanghaiNow(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("business clock must be timezone-aware")
    return value.astimezone(SHANGHAI)


def PhaseAt(value: datetime, *, is_trading_day: bool) -> MarketPhase:
    if not is_trading_day:
        return MarketPhase.CLOSED
    current = ShanghaiNow(value).time().replace(tzinfo=None)
    if current >= FREEZE_TIME:
        return MarketPhase.AFTER_CLOSE
    return MarketPhase.BEFORE_CLOSE if current >= time(9, 15) else MarketPhase.CLOSED


def DecisionAt(value: datetime, *, is_trading_day: bool) -> ScheduleDecision:
    phase = PhaseAt(value, is_trading_day=is_trading_day)
    active = phase is MarketPhase.BEFORE_CLOSE
    return ScheduleDecision(
        phase=phase,
        should_refresh_market=active,
        should_score=active,
        should_review=active,
        freeze_strategies=_freeze_at(value, is_trading_day=is_trading_day),
    )


def TradeDateAt(value: datetime) -> date:
    return ShanghaiNow(value).date()


def FreezeDueAt(value: datetime, *, is_trading_day: bool) -> tuple[str, ...]:
    if not is_trading_day:
        return ()
    current = ShanghaiNow(value).time().replace(tzinfo=None)
    if current >= FREEZE_TIME:
        return ("tomorrow", "d25")
    return ()


def StartupFreezeStrategies(value: datetime, *, is_trading_day: bool) -> tuple[str, ...]:
    """Return same-day recovery freezes for initialization before close fallback."""

    if not is_trading_day:
        return ()
    current = ShanghaiNow(value).time().replace(tzinfo=None)
    if current >= FREEZE_TIME:
        return ("tomorrow", "d25")
    return ()


def SchedulePointAt(value: datetime, *, is_trading_day: bool) -> SchedulePoint | None:
    if not is_trading_day:
        return None
    current = ShanghaiNow(value).time().replace(tzinfo=None)
    return SchedulePoint.AFTERNOON_FREEZE if current.replace(microsecond=0) == FREEZE_TIME else None


def SecondsUntilNextScheduleBoundary(value: datetime, *, maximum_seconds: float) -> float:
    local = ShanghaiNow(value)
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
    point = SchedulePointAt(value, is_trading_day=is_trading_day)
    if point is SchedulePoint.AFTERNOON_FREEZE:
        return ("tomorrow", "d25")
    return ()


__all__ = [
    "MarketPhase",
    "SHANGHAI",
    "SchedulePoint",
    "ScheduleDecision",
    "DecisionAt",
    "FreezeDueAt",
    "PhaseAt",
    "SchedulePointAt",
    "SecondsUntilNextScheduleBoundary",
    "ShanghaiNow",
    "StartupFreezeStrategies",
    "TradeDateAt",
]
