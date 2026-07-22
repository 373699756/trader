"""Stateful no-catch-up cadence planning for production pipeline tasks."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from types import MappingProxyType

from trader.application.schedule import (
    MarketPhase,
    SchedulePoint,
    phase_at,
    seconds_until_next_schedule_boundary,
    shanghai_now,
)


class CadenceBand(str, Enum):
    WARMUP = "warmup"
    TODAY_MAIN = "today_main"
    TODAY_LATE = "today_late"
    MIDDAY = "midday"
    AFTERNOON = "afternoon"
    FINAL_REVIEW = "final_review"
    FINAL_WINDOW = "final_window"
    AFTER_CLOSE = "after_close"
    CLOSED = "closed"


class PipelineTask(str, Enum):
    FULL_MARKET = "full_market"
    CANDIDATE_QUOTES = "candidate_quotes"
    TOPK_QUOTES = "topk_quotes"
    SCORE = "score"
    INDUSTRY_HEAT = "industry_heat"
    MARKET_NEWS = "market_news"
    STOCK_RISK = "stock_risk"
    REFERENCE_DATA = "reference_data"
    DEEPSEEK_CUTOFF = "deepseek_cutoff"
    FINAL_CANDIDATE_QUOTES = "final_candidate_quotes"
    FREEZE = "freeze"
    CLOSE_QUOTES = "close_quotes"
    CURRENT_QUOTES = "current_quotes"


def task_execution_budget_seconds(task: PipelineTask) -> float | None:
    return {
        PipelineTask.FULL_MARKET: 20.0,
        PipelineTask.CANDIDATE_QUOTES: 3.0,
        PipelineTask.TOPK_QUOTES: 3.0,
        PipelineTask.SCORE: 15.0,
        PipelineTask.INDUSTRY_HEAT: 20.0,
        PipelineTask.MARKET_NEWS: 8.0,
        PipelineTask.STOCK_RISK: 8.0,
        PipelineTask.REFERENCE_DATA: 20.0,
        PipelineTask.DEEPSEEK_CUTOFF: 1.0,
        PipelineTask.FINAL_CANDIDATE_QUOTES: 10.0,
        PipelineTask.CLOSE_QUOTES: 60.0,
        PipelineTask.CURRENT_QUOTES: 20.0,
        PipelineTask.FREEZE: None,
    }[task]


PERIODIC_TASKS = (
    PipelineTask.FULL_MARKET,
    PipelineTask.CANDIDATE_QUOTES,
    PipelineTask.TOPK_QUOTES,
    PipelineTask.SCORE,
    PipelineTask.INDUSTRY_HEAT,
    PipelineTask.MARKET_NEWS,
    PipelineTask.STOCK_RISK,
)


@dataclass(frozen=True)
class CadencePolicy:
    intervals: Mapping[PipelineTask, Mapping[CadenceBand, float]]

    def __post_init__(self) -> None:
        if set(self.intervals) != set(PERIODIC_TASKS):
            raise ValueError("cadence policy must define every periodic pipeline task")
        normalized: dict[PipelineTask, Mapping[CadenceBand, float]] = {}
        for task, values in self.intervals.items():
            if not values or any(interval <= 0.0 for interval in values.values()):
                raise ValueError(f"cadence intervals for {task.value} must be positive")
            if CadenceBand.CLOSED in values or CadenceBand.AFTER_CLOSE in values:
                raise ValueError(f"cadence intervals for {task.value} cannot run outside the trading timeline")
            normalized[task] = MappingProxyType(dict(values))
        object.__setattr__(self, "intervals", MappingProxyType(normalized))

    @classmethod
    def from_seconds(cls, raw: Mapping[str, Mapping[str, float]]) -> CadencePolicy:
        try:
            intervals = {
                PipelineTask(task): {CadenceBand(band): float(seconds) for band, seconds in values.items()}
                for task, values in raw.items()
            }
        except ValueError as exc:
            raise ValueError("cadence policy contains an unknown task or phase band") from exc
        return cls(intervals)

    def interval(self, task: PipelineTask, band: CadenceBand) -> float | None:
        return self.intervals.get(task, {}).get(band)


@dataclass(frozen=True)
class ScheduledPipelineTask:
    task: PipelineTask
    scheduled_at: datetime
    phase: MarketPhase
    freeze_strategies: tuple[str, ...] = ()


@dataclass(frozen=True)
class CadenceBatch:
    tasks: tuple[ScheduledPipelineTask, ...]
    next_delay_seconds: float


class CadencePlanner:
    def __init__(self, policy: CadencePolicy) -> None:
        self._policy = policy
        self._lock = threading.Lock()
        self._next_due: dict[tuple[str, CadenceBand, PipelineTask], datetime] = {}
        self._fired_points: set[tuple[str, SchedulePoint]] = set()
        self._reference_dates: set[str] = set()

    def plan(self, at: datetime, *, is_trading_day: bool) -> CadenceBatch:
        with self._lock:
            return self._plan_locked(at, is_trading_day=is_trading_day)

    def status(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "intervals": {
                    task.value: {band.value: seconds for band, seconds in values.items()}
                    for task, values in self._policy.intervals.items()
                },
                "next_due": {
                    f"{trade_date}:{band.value}:{task.value}": due.isoformat()
                    for (trade_date, band, task), due in self._next_due.items()
                },
                "fired_points": sorted(f"{trade_date}:{point.value}" for trade_date, point in self._fired_points),
            }

    def interval_for(self, task: PipelineTask, at: datetime, *, is_trading_day: bool) -> float | None:
        if not is_trading_day:
            return None
        return self._policy.interval(task, cadence_band(phase_at(shanghai_now(at), is_trading_day=True)))

    def _plan_locked(self, at: datetime, *, is_trading_day: bool) -> CadenceBatch:
        local = shanghai_now(at)
        trade_date = local.date().isoformat()
        phase = phase_at(local, is_trading_day=is_trading_day)
        band = cadence_band(phase)
        self._discard_old_state(trade_date, band)
        if not is_trading_day or band is CadenceBand.CLOSED:
            return CadenceBatch((), seconds_until_next_schedule_boundary(local, maximum_seconds=30.0))

        tasks: list[ScheduledPipelineTask] = []
        if trade_date not in self._reference_dates:
            self._reference_dates.add(trade_date)
            tasks.append(ScheduledPipelineTask(PipelineTask.REFERENCE_DATA, local, phase))
            if band in {CadenceBand.FINAL_WINDOW, CadenceBand.AFTER_CLOSE}:
                tasks.append(ScheduledPipelineTask(PipelineTask.CURRENT_QUOTES, local, phase))
        due_points = _due_schedule_points(local)
        for point in due_points:
            if (trade_date, point) in self._fired_points:
                continue
            self._fired_points.add((trade_date, point))
            tasks.extend(_point_tasks(point, local, phase))
        tasks = list(_combine_freeze_tasks(tasks))

        point_task_names = {item.task for item in tasks}
        for task in PERIODIC_TASKS:
            interval = self._policy.interval(task, band)
            if interval is None:
                continue
            if SchedulePoint.FINAL_CANDIDATE_QUOTES in due_points and task is PipelineTask.CANDIDATE_QUOTES:
                continue
            key = (trade_date, band, task)
            due = self._next_due.get(key)
            if due is None or local >= due:
                if task not in point_task_names:
                    tasks.append(ScheduledPipelineTask(task, local, phase))
                self._next_due[key] = local + timedelta(seconds=interval)

        next_delays = tuple(
            max(0.05, (due - local).total_seconds())
            for (date_key, band_key, _task), due in self._next_due.items()
            if date_key == trade_date and band_key is band and due > local
        )
        maximum = min(next_delays, default=30.0)
        delay = seconds_until_next_schedule_boundary(local, maximum_seconds=maximum)
        return CadenceBatch(tuple(tasks), delay)

    def _discard_old_state(self, trade_date: str, band: CadenceBand) -> None:
        self._next_due = {key: due for key, due in self._next_due.items() if key[0] == trade_date and key[1] is band}
        self._fired_points = {key for key in self._fired_points if key[0] == trade_date}
        self._reference_dates = {value for value in self._reference_dates if value == trade_date}


def cadence_band(phase: MarketPhase) -> CadenceBand:
    return {
        MarketPhase.CLOSED: CadenceBand.CLOSED,
        MarketPhase.WARMUP: CadenceBand.WARMUP,
        MarketPhase.TODAY_OBSERVE: CadenceBand.TODAY_MAIN,
        MarketPhase.TODAY_MAIN: CadenceBand.TODAY_MAIN,
        MarketPhase.TODAY_LATE: CadenceBand.TODAY_LATE,
        MarketPhase.MIDDAY: CadenceBand.MIDDAY,
        MarketPhase.AFTERNOON: CadenceBand.AFTERNOON,
        MarketPhase.FINAL_REVIEW: CadenceBand.FINAL_REVIEW,
        MarketPhase.DEEPSEEK_CUTOFF: CadenceBand.FINAL_WINDOW,
        MarketPhase.FINAL_QUOTE: CadenceBand.FINAL_WINDOW,
        MarketPhase.FROZEN: CadenceBand.FINAL_WINDOW,
        MarketPhase.AFTER_CLOSE: CadenceBand.AFTER_CLOSE,
    }[phase]


def freshness_level(age_seconds: float | None, interval_seconds: float | None) -> str:
    if age_seconds is None or interval_seconds is None:
        return "unavailable"
    if interval_seconds <= 0.0:
        raise ValueError("freshness interval must be positive")
    if age_seconds > interval_seconds * 3.0:
        return "degraded"
    if age_seconds > interval_seconds * 2.0:
        return "stale"
    return "fresh"


def _due_schedule_points(local: datetime) -> tuple[SchedulePoint, ...]:
    current = local.time().replace(tzinfo=None)
    points: list[SchedulePoint] = []
    if current >= time(11, 20):
        points.append(SchedulePoint.TODAY_FREEZE)
    if current >= time(14, 48):
        points.append(SchedulePoint.DEEPSEEK_CUTOFF)
    if time(14, 49, 50) <= current < time(14, 50):
        points.append(SchedulePoint.FINAL_CANDIDATE_QUOTES)
    if current >= time(14, 50):
        points.append(SchedulePoint.AFTERNOON_FREEZE)
    if current >= time(15, 0):
        points.append(SchedulePoint.CLOSE_QUOTES)
    return tuple(points)


def _combine_freeze_tasks(tasks: list[ScheduledPipelineTask]) -> tuple[ScheduledPipelineTask, ...]:
    freezes = [item for item in tasks if item.task is PipelineTask.FREEZE]
    if len(freezes) < 2:
        return tuple(tasks)
    strategies = tuple(dict.fromkeys(strategy for item in freezes for strategy in item.freeze_strategies))
    combined = ScheduledPipelineTask(
        PipelineTask.FREEZE,
        max(item.scheduled_at for item in freezes),
        freezes[-1].phase,
        strategies,
    )
    result = [item for item in tasks if item.task is not PipelineTask.FREEZE]
    result.append(combined)
    return tuple(result)


def _point_tasks(
    point: SchedulePoint,
    at: datetime,
    phase: MarketPhase,
) -> tuple[ScheduledPipelineTask, ...]:
    if point is SchedulePoint.TODAY_FREEZE:
        return (ScheduledPipelineTask(PipelineTask.FREEZE, at, phase, ("today",)),)
    if point is SchedulePoint.DEEPSEEK_CUTOFF:
        return (ScheduledPipelineTask(PipelineTask.DEEPSEEK_CUTOFF, at, phase),)
    if point is SchedulePoint.FINAL_CANDIDATE_QUOTES:
        return (ScheduledPipelineTask(PipelineTask.FINAL_CANDIDATE_QUOTES, at, phase),)
    if point is SchedulePoint.AFTERNOON_FREEZE:
        return (ScheduledPipelineTask(PipelineTask.FREEZE, at, phase, ("tomorrow", "d25")),)
    return (
        ScheduledPipelineTask(PipelineTask.CLOSE_QUOTES, at, phase),
        ScheduledPipelineTask(PipelineTask.REFERENCE_DATA, at, phase),
    )


__all__ = [
    "CadenceBand",
    "CadenceBatch",
    "CadencePlanner",
    "CadencePolicy",
    "PERIODIC_TASKS",
    "PipelineTask",
    "ScheduledPipelineTask",
    "cadence_band",
    "freshness_level",
]
