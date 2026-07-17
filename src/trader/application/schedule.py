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


def _freeze_at(value: datetime, *, is_trading_day: bool) -> tuple[str, ...]:
    if not is_trading_day:
        return ()
    current = shanghai_now(value).time().replace(tzinfo=None)
    if time(11, 20) <= current < time(11, 21):
        return ("today",)
    if time(14, 50) <= current < time(14, 51):
        return ("tomorrow", "d25")
    return ()


__all__ = [
    "MarketPhase",
    "SHANGHAI",
    "ScheduleDecision",
    "decision_at",
    "freeze_due_at",
    "phase_at",
    "shanghai_now",
    "trade_date_at",
]
