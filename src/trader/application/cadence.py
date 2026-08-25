"""Stateful no-catch-up cadence planning for production pipeline tasks."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from enum import Enum
from types import MappingProxyType

from trader.application.schedule import (
    MarketPhase,
    SchedulePoint,
    decision_at,
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
    INTRADAY_TAIL = "intraday_tail"
    LONG_QUOTES = "long_quotes"
    SCORE = "score"
    INDUSTRY_HEAT = "industry_heat"
    MARKET_NEWS = "market_news"
    STOCK_RISK = "stock_risk"
    REFERENCE_DATA = "reference_data"
    DEEPSEEK_CUTOFF = "deepseek_cutoff"
    CHECKPOINT = "checkpoint"
    FINAL_CANDIDATE_QUOTES = "final_candidate_quotes"
    FREEZE = "freeze"
    CLOSE_QUOTES = "close_quotes"
    CURRENT_QUOTES = "current_quotes"


class SchedulePointLifecycle(str, Enum):
    PENDING = "pending"
    INFLIGHT = "inflight"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    MISSED = "missed"


class SchedulePointResult(str, Enum):
    COMPLETED = "completed"
    RETRY = "retry"
    MISSED = "missed"


@dataclass(frozen=True, order=True)
class SchedulePointKey:
    trade_date: str
    schedule_point: SchedulePoint
    strategy: str

    def __post_init__(self) -> None:
        date.fromisoformat(self.trade_date)
        if not self.strategy:
            raise ValueError("schedule point strategy cannot be empty")

    @property
    def label(self) -> str:
        return f"{self.trade_date}:{self.schedule_point.value}:{self.strategy}"


@dataclass(frozen=True)
class SchedulePointState:
    lifecycle: SchedulePointLifecycle
    attempt_count: int
    updated_at: datetime
    next_retry_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.attempt_count < 0:
            raise ValueError("schedule point attempt count cannot be negative")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("schedule point update time must be timezone-aware")
        if self.next_retry_at is not None and (
            self.next_retry_at.tzinfo is None or self.next_retry_at.utcoffset() is None
        ):
            raise ValueError("schedule point retry time must be timezone-aware")


def task_execution_budget_seconds(task: PipelineTask) -> float | None:
    return {
        PipelineTask.FULL_MARKET: 20.0,
        PipelineTask.CANDIDATE_QUOTES: 3.0,
        PipelineTask.TOPK_QUOTES: 3.0,
        PipelineTask.INTRADAY_TAIL: 3.0,
        PipelineTask.LONG_QUOTES: 3.0,
        PipelineTask.SCORE: 15.0,
        PipelineTask.INDUSTRY_HEAT: 20.0,
        PipelineTask.MARKET_NEWS: 8.0,
        PipelineTask.STOCK_RISK: 8.0,
        PipelineTask.REFERENCE_DATA: 20.0,
        PipelineTask.DEEPSEEK_CUTOFF: 1.0,
        PipelineTask.CHECKPOINT: None,
        PipelineTask.FINAL_CANDIDATE_QUOTES: 10.0,
        PipelineTask.CLOSE_QUOTES: 180.0,
        PipelineTask.CURRENT_QUOTES: 20.0,
        PipelineTask.FREEZE: None,
    }[task]


PERIODIC_TASKS = (
    PipelineTask.FULL_MARKET,
    PipelineTask.CANDIDATE_QUOTES,
    PipelineTask.TOPK_QUOTES,
    PipelineTask.INTRADAY_TAIL,
    PipelineTask.LONG_QUOTES,
    PipelineTask.INDUSTRY_HEAT,
    PipelineTask.MARKET_NEWS,
    PipelineTask.STOCK_RISK,
)
_CADENCE_TASKS = (*PERIODIC_TASKS, PipelineTask.SCORE)


@dataclass(frozen=True)
class CadencePolicy:
    intervals: Mapping[PipelineTask, Mapping[CadenceBand, float]]

    def __post_init__(self) -> None:
        if set(self.intervals) != set(_CADENCE_TASKS):
            raise ValueError("cadence policy must define every periodic or input-driven pipeline task")
        normalized: dict[PipelineTask, Mapping[CadenceBand, float]] = {}
        for task, values in self.intervals.items():
            if not values or any(interval <= 0.0 for interval in values.values()):
                raise ValueError(f"cadence intervals for {task.value} must be positive")
            if CadenceBand.CLOSED in values:
                raise ValueError(f"cadence intervals for {task.value} cannot run while the market is closed")
            if CadenceBand.AFTER_CLOSE in values and task is not PipelineTask.TOPK_QUOTES:
                raise ValueError("only selected-code quote overlays may run after close")
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
    schedule_point: SchedulePoint | None = None


@dataclass(frozen=True)
class CadenceBatch:
    tasks: tuple[ScheduledPipelineTask, ...]
    next_delay_seconds: float


@dataclass(frozen=True)
class CadencePlannerStatus:
    started_at: datetime | None
    intervals: Mapping[PipelineTask, Mapping[CadenceBand, float]]
    next_due: Mapping[tuple[str, CadenceBand, PipelineTask], datetime]
    schedule_points: Mapping[SchedulePointKey, SchedulePointState]
    fired_points: tuple[tuple[str, SchedulePoint], ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "intervals",
            MappingProxyType({task: MappingProxyType(dict(values)) for task, values in self.intervals.items()}),
        )
        object.__setattr__(self, "next_due", MappingProxyType(dict(self.next_due)))
        object.__setattr__(self, "schedule_points", MappingProxyType(dict(self.schedule_points)))


class CadencePlanner:
    def __init__(self, policy: CadencePolicy, *, started_at: datetime | None = None) -> None:
        self._policy = policy
        self._lock = threading.Lock()
        self._next_due: dict[tuple[str, CadenceBand, PipelineTask], datetime] = {}
        self._started_at = shanghai_now(started_at) if started_at is not None else None
        self._point_states: dict[SchedulePointKey, SchedulePointState] = {}
        self._reference_dates: set[str] = set()

    def plan(self, at: datetime, *, is_trading_day: bool) -> CadenceBatch:
        with self._lock:
            return self._plan_locked(at, is_trading_day=is_trading_day)

    def plan_score_after_input(
        self,
        at: datetime,
        *,
        is_trading_day: bool,
    ) -> ScheduledPipelineTask | None:
        local = shanghai_now(at)
        decision = decision_at(local, is_trading_day=is_trading_day)
        if not decision.should_score:
            return None
        phase = decision.phase
        band = cadence_band(phase)
        interval = self._policy.interval(PipelineTask.SCORE, band)
        if interval is None:
            return None
        key = (local.date().isoformat(), band, PipelineTask.SCORE)
        with self._lock:
            due = self._next_due.get(key)
            if due is not None and local < due:
                return None
            self._next_due[key] = local + timedelta(seconds=interval)
        return ScheduledPipelineTask(PipelineTask.SCORE, local, phase)

    def status(self) -> CadencePlannerStatus:
        with self._lock:
            return CadencePlannerStatus(
                started_at=self._started_at,
                intervals=self._policy.intervals,
                next_due=self._next_due,
                schedule_points=self._point_states,
                fired_points=tuple(
                    sorted(
                        {
                            (key.trade_date, key.schedule_point)
                            for key, state in self._point_states.items()
                            if state.lifecycle is SchedulePointLifecycle.COMPLETED
                        }
                    )
                ),
            )

    def schedule_point_lifecycle(
        self,
        trade_date: str,
        schedule_point: SchedulePoint,
        strategy: str = "-",
    ) -> SchedulePointLifecycle | None:
        with self._lock:
            state = self._point_states.get(SchedulePointKey(trade_date, schedule_point, strategy))
            return state.lifecycle if state is not None else None

    def record_submission(
        self,
        scheduled: ScheduledPipelineTask,
        *,
        accepted: bool,
        at: datetime,
    ) -> None:
        local = shanghai_now(at)
        with self._lock:
            for key in _scheduled_point_keys(scheduled):
                current = self._point_states.get(key)
                if current is None or current.lifecycle not in {
                    SchedulePointLifecycle.PENDING,
                    SchedulePointLifecycle.RETRY_WAIT,
                }:
                    continue
                self._point_states[key] = replace(
                    current,
                    lifecycle=(SchedulePointLifecycle.INFLIGHT if accepted else SchedulePointLifecycle.PENDING),
                    updated_at=local,
                    next_retry_at=None if accepted else current.next_retry_at,
                )

    def record_results(
        self,
        scheduled: ScheduledPipelineTask,
        results: Mapping[str, SchedulePointResult],
        *,
        at: datetime,
    ) -> None:
        local = shanghai_now(at)
        with self._lock:
            for key in _scheduled_point_keys(scheduled):
                raw_result = results.get(key.strategy, results.get("-", results.get("")))
                if raw_result is None:
                    continue
                current = self._point_states.get(key)
                if current is None:
                    continue
                if raw_result is SchedulePointResult.COMPLETED:
                    updated = replace(
                        current,
                        lifecycle=SchedulePointLifecycle.COMPLETED,
                        updated_at=local,
                        next_retry_at=None,
                    )
                elif raw_result is SchedulePointResult.MISSED:
                    updated = replace(
                        current,
                        lifecycle=SchedulePointLifecycle.MISSED,
                        updated_at=local,
                        next_retry_at=None,
                    )
                else:
                    attempt_count = current.attempt_count + 1
                    delay = _schedule_retry_delay(attempt_count)
                    updated = replace(
                        current,
                        lifecycle=SchedulePointLifecycle.RETRY_WAIT,
                        attempt_count=attempt_count,
                        updated_at=local,
                        next_retry_at=local + timedelta(seconds=delay),
                    )
                self._point_states[key] = updated

    def record_point_result(
        self,
        trade_date: str,
        schedule_point: SchedulePoint,
        strategy: str,
        result: SchedulePointResult,
        *,
        at: datetime,
    ) -> None:
        local = shanghai_now(at)
        key = SchedulePointKey(trade_date, schedule_point, strategy)
        with self._lock:
            current = self._point_states.get(key)
            if current is None:
                current = SchedulePointState(SchedulePointLifecycle.INFLIGHT, 0, local)
                self._point_states[key] = current
            if result is SchedulePointResult.COMPLETED:
                lifecycle = SchedulePointLifecycle.COMPLETED
                next_retry_at = None
                attempt_count = current.attempt_count
            elif result is SchedulePointResult.MISSED:
                lifecycle = SchedulePointLifecycle.MISSED
                next_retry_at = None
                attempt_count = current.attempt_count
            else:
                lifecycle = SchedulePointLifecycle.RETRY_WAIT
                attempt_count = current.attempt_count + 1
                next_retry_at = local + timedelta(seconds=_schedule_retry_delay(attempt_count))
            self._point_states[key] = SchedulePointState(
                lifecycle,
                attempt_count,
                local,
                next_retry_at,
            )

    def rotate_session(self, at: datetime, *, reason: str) -> None:
        if not reason:
            raise ValueError("cadence rotation reason cannot be empty")
        local = shanghai_now(at)
        with self._lock:
            for key, state in tuple(self._point_states.items()):
                if state.lifecycle not in {
                    SchedulePointLifecycle.PENDING,
                    SchedulePointLifecycle.INFLIGHT,
                    SchedulePointLifecycle.RETRY_WAIT,
                }:
                    continue
                if key.schedule_point in {
                    SchedulePoint.TODAY_FREEZE,
                    SchedulePoint.AFTERNOON_FREEZE,
                }:
                    continue
                self._point_states[key] = replace(
                    state,
                    lifecycle=SchedulePointLifecycle.MISSED,
                    updated_at=local,
                    next_retry_at=None,
                )
            self._next_due.clear()

    def interval_for(self, task: PipelineTask, at: datetime, *, is_trading_day: bool) -> float | None:
        if not is_trading_day:
            return None
        return self._policy.interval(task, cadence_band(phase_at(shanghai_now(at), is_trading_day=True)))

    def has_active_afternoon_freeze(self, trade_date: str) -> bool:
        with self._lock:
            return self._afternoon_freeze_active(trade_date)

    def _plan_locked(self, at: datetime, *, is_trading_day: bool) -> CadenceBatch:
        local = shanghai_now(at)
        if self._started_at is None:
            self._started_at = local
        trade_date = local.date().isoformat()
        phase = phase_at(local, is_trading_day=is_trading_day)
        band = cadence_band(phase)
        self._discard_old_state(trade_date, band)
        if not is_trading_day or band is CadenceBand.CLOSED:
            return CadenceBatch((), seconds_until_next_schedule_boundary(local, maximum_seconds=30.0))

        self._synchronize_schedule_points(local)
        tasks: list[ScheduledPipelineTask] = []
        if trade_date not in self._reference_dates:
            self._reference_dates.add(trade_date)
            tasks.append(ScheduledPipelineTask(PipelineTask.REFERENCE_DATA, local, phase))
            if band is CadenceBand.FINAL_WINDOW:
                tasks.append(ScheduledPipelineTask(PipelineTask.CURRENT_QUOTES, local, phase))
        due_points = self._due_schedule_points(local)
        for point, strategies in due_points:
            tasks.extend(_point_tasks(point, local, phase, strategies=strategies))
        tasks = list(_combine_freeze_tasks(tasks))
        if self._afternoon_freeze_active(trade_date):
            tasks = [task for task in tasks if task.task is not PipelineTask.CLOSE_QUOTES]
        self._append_periodic_tasks(tasks, local, phase, tuple(point for point, _strategies in due_points))

        next_delays = tuple(
            max(0.05, (due - local).total_seconds())
            for (date_key, band_key, _task), due in self._next_due.items()
            if date_key == trade_date and band_key is band and due > local
        )
        retry_delays = tuple(
            max(0.05, (state.next_retry_at - local).total_seconds())
            for key, state in self._point_states.items()
            if key.trade_date == trade_date
            and state.lifecycle is SchedulePointLifecycle.RETRY_WAIT
            and state.next_retry_at is not None
            and state.next_retry_at > local
        )
        maximum = min((*next_delays, *retry_delays), default=30.0)
        delay = seconds_until_next_schedule_boundary(local, maximum_seconds=maximum)
        return CadenceBatch(tuple(tasks), delay)

    def _synchronize_schedule_points(self, local: datetime) -> None:
        assert self._started_at is not None
        trade_date = local.date().isoformat()
        started = self._started_at
        for point, strategies in _schedule_point_strategies():
            boundary = _point_boundary(local, point)
            for strategy in strategies:
                key = SchedulePointKey(trade_date, point, strategy)
                current = self._point_states.get(key)
                if current is not None:
                    if current.lifecycle in {
                        SchedulePointLifecycle.PENDING,
                        SchedulePointLifecycle.INFLIGHT,
                        SchedulePointLifecycle.RETRY_WAIT,
                    } and _point_window_expired(point, local):
                        self._point_states[key] = replace(
                            current,
                            lifecycle=SchedulePointLifecycle.MISSED,
                            updated_at=local,
                            next_retry_at=None,
                        )
                    continue
                lifecycle = _initial_point_lifecycle(
                    point,
                    local=local,
                    boundary=boundary,
                    started_at=started,
                )
                self._point_states[key] = SchedulePointState(lifecycle, 0, local)

    def _due_schedule_points(
        self,
        local: datetime,
    ) -> tuple[tuple[SchedulePoint, tuple[str, ...]], ...]:
        trade_date = local.date().isoformat()
        grouped: list[tuple[SchedulePoint, tuple[str, ...]]] = []
        for point, strategies in _schedule_point_strategies():
            if local < _point_boundary(local, point):
                continue
            due_strategies: list[str] = []
            for strategy in strategies:
                state = self._point_states[SchedulePointKey(trade_date, point, strategy)]
                if state.lifecycle is SchedulePointLifecycle.PENDING:
                    due_strategies.append(strategy)
                elif (
                    state.lifecycle is SchedulePointLifecycle.RETRY_WAIT
                    and state.next_retry_at is not None
                    and local >= state.next_retry_at
                ):
                    due_strategies.append(strategy)
            if due_strategies:
                grouped.append((point, tuple(due_strategies)))
        return tuple(grouped)

    def _afternoon_freeze_active(self, trade_date: str) -> bool:
        return any(
            key.trade_date == trade_date
            and key.schedule_point is SchedulePoint.AFTERNOON_FREEZE
            and state.lifecycle
            in {
                SchedulePointLifecycle.PENDING,
                SchedulePointLifecycle.INFLIGHT,
                SchedulePointLifecycle.RETRY_WAIT,
            }
            for key, state in self._point_states.items()
        )

    def _append_periodic_tasks(
        self,
        tasks: list[ScheduledPipelineTask],
        local: datetime,
        phase: MarketPhase,
        due_points: tuple[SchedulePoint, ...],
    ) -> None:
        trade_date = local.date().isoformat()
        band = cadence_band(phase)
        point_task_names = {item.task for item in tasks}
        final_quotes_due = (
            SchedulePoint.FINAL_CANDIDATE_QUOTES in due_points
            or phase is MarketPhase.FINAL_QUOTE
            or self._quote_checkpoint_active(trade_date, local)
        )
        for task in PERIODIC_TASKS:
            if phase is MarketPhase.FROZEN and task not in {PipelineTask.TOPK_QUOTES, PipelineTask.LONG_QUOTES}:
                continue
            interval = self._policy.interval(task, band)
            if interval is None or (final_quotes_due and task is PipelineTask.CANDIDATE_QUOTES):
                continue
            key = (trade_date, band, task)
            due = self._next_due.get(key)
            if due is not None and local < due:
                continue
            if task not in point_task_names:
                tasks.append(ScheduledPipelineTask(task, local, phase))
            self._next_due[key] = local + timedelta(seconds=interval)

    def _quote_checkpoint_active(self, trade_date: str, local: datetime) -> bool:
        return any(
            key.trade_date == trade_date
            and key.schedule_point in {SchedulePoint.TODAY_CHECKPOINT, SchedulePoint.FINAL_CANDIDATE_QUOTES}
            and local >= _point_boundary(local, key.schedule_point)
            and state.lifecycle
            in {
                SchedulePointLifecycle.PENDING,
                SchedulePointLifecycle.INFLIGHT,
                SchedulePointLifecycle.RETRY_WAIT,
            }
            for key, state in self._point_states.items()
        )

    def _discard_old_state(self, trade_date: str, band: CadenceBand) -> None:
        self._next_due = {key: due for key, due in self._next_due.items() if key[0] == trade_date and key[1] is band}
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


def _schedule_point_strategies() -> tuple[tuple[SchedulePoint, tuple[str, ...]], ...]:
    return (
        (SchedulePoint.TODAY_CHECKPOINT, ("today",)),
        (SchedulePoint.TODAY_FREEZE, ("today",)),
        (SchedulePoint.DEEPSEEK_CUTOFF, ("-",)),
        (SchedulePoint.AFTERNOON_CHECKPOINT, ("tomorrow", "d25")),
        (SchedulePoint.FINAL_CANDIDATE_QUOTES, ("-",)),
        (SchedulePoint.AFTERNOON_FREEZE, ("tomorrow", "d25")),
        (SchedulePoint.CLOSE_QUOTES, ("-",)),
    )


def _point_boundary(local: datetime, point: SchedulePoint) -> datetime:
    raw = {
        SchedulePoint.TODAY_CHECKPOINT: time(11, 19, 50),
        SchedulePoint.TODAY_FREEZE: time(11, 20),
        SchedulePoint.DEEPSEEK_CUTOFF: time(14, 48),
        SchedulePoint.AFTERNOON_CHECKPOINT: time(14, 49, 20),
        SchedulePoint.FINAL_CANDIDATE_QUOTES: time(14, 49, 50),
        SchedulePoint.AFTERNOON_FREEZE: time(14, 50),
        SchedulePoint.CLOSE_QUOTES: time(15, 0),
    }[point]
    return local.replace(hour=raw.hour, minute=raw.minute, second=raw.second, microsecond=0)


def _initial_point_lifecycle(
    point: SchedulePoint,
    *,
    local: datetime,
    boundary: datetime,
    started_at: datetime,
) -> SchedulePointLifecycle:
    lifecycle = SchedulePointLifecycle.MISSED
    if local < boundary:
        lifecycle = SchedulePointLifecycle.PENDING
    else:
        current = local.time().replace(tzinfo=None)
        started_before_boundary = started_at.date() < boundary.date() or started_at < boundary
        if point is SchedulePoint.TODAY_CHECKPOINT:
            eligible = current < time(11, 20) and started_at <= boundary
        elif point is SchedulePoint.TODAY_FREEZE:
            eligible = started_before_boundary and current < time(11, 21)
        elif point in {SchedulePoint.DEEPSEEK_CUTOFF, SchedulePoint.FINAL_CANDIDATE_QUOTES}:
            eligible = current < time(14, 50) and started_at <= boundary
        elif point is SchedulePoint.AFTERNOON_CHECKPOINT:
            eligible = current < time(14, 50)
        elif point is SchedulePoint.AFTERNOON_FREEZE:
            eligible = current < time(15, 0) or started_before_boundary
        else:
            eligible = True
        if eligible:
            lifecycle = SchedulePointLifecycle.PENDING
    return lifecycle


def _point_window_expired(point: SchedulePoint, local: datetime) -> bool:
    current = local.time().replace(tzinfo=None)
    if point is SchedulePoint.TODAY_CHECKPOINT:
        return current >= time(11, 20)
    if point is SchedulePoint.TODAY_FREEZE:
        return current >= time(11, 21)
    if point in {
        SchedulePoint.DEEPSEEK_CUTOFF,
        SchedulePoint.AFTERNOON_CHECKPOINT,
        SchedulePoint.FINAL_CANDIDATE_QUOTES,
    }:
        return current >= time(14, 50)
    return False


def _combine_freeze_tasks(tasks: list[ScheduledPipelineTask]) -> tuple[ScheduledPipelineTask, ...]:
    freezes = [item for item in tasks if item.task is PipelineTask.FREEZE]
    if len(freezes) < 2:
        return tuple(tasks)
    if len({item.schedule_point for item in freezes}) != 1:
        return tuple(tasks)
    strategies = tuple(dict.fromkeys(strategy for item in freezes for strategy in item.freeze_strategies))
    combined = ScheduledPipelineTask(
        PipelineTask.FREEZE,
        max(item.scheduled_at for item in freezes),
        freezes[-1].phase,
        strategies,
        max(
            (item.schedule_point for item in freezes if item.schedule_point is not None),
            key=lambda point: _schedule_point_order(point),
            default=None,
        ),
    )
    result = [item for item in tasks if item.task is not PipelineTask.FREEZE]
    result.append(combined)
    return tuple(result)


def _point_tasks(
    point: SchedulePoint,
    at: datetime,
    phase: MarketPhase,
    *,
    strategies: tuple[str, ...],
) -> tuple[ScheduledPipelineTask, ...]:
    tasks: tuple[ScheduledPipelineTask, ...]
    if point is SchedulePoint.TODAY_CHECKPOINT:
        tasks = (ScheduledPipelineTask(PipelineTask.FINAL_CANDIDATE_QUOTES, at, phase, (), point),)
    elif point in {SchedulePoint.TODAY_FREEZE, SchedulePoint.AFTERNOON_FREEZE}:
        tasks = (ScheduledPipelineTask(PipelineTask.FREEZE, at, phase, strategies, point),)
    elif point is SchedulePoint.DEEPSEEK_CUTOFF:
        tasks = (ScheduledPipelineTask(PipelineTask.DEEPSEEK_CUTOFF, at, phase, (), point),)
    elif point is SchedulePoint.AFTERNOON_CHECKPOINT:
        tasks = (ScheduledPipelineTask(PipelineTask.CHECKPOINT, at, phase, strategies, point),)
    elif point is SchedulePoint.FINAL_CANDIDATE_QUOTES:
        tasks = (ScheduledPipelineTask(PipelineTask.FINAL_CANDIDATE_QUOTES, at, phase, (), point),)
    elif point is SchedulePoint.CLOSE_QUOTES:
        tasks = (
            ScheduledPipelineTask(PipelineTask.CLOSE_QUOTES, at, phase, (), point),
            ScheduledPipelineTask(PipelineTask.LONG_QUOTES, at, phase),
            ScheduledPipelineTask(PipelineTask.REFERENCE_DATA, at, phase),
        )
    else:
        raise ValueError(f"unsupported schedule point: {point.value}")
    return tasks


def _scheduled_point_keys(scheduled: ScheduledPipelineTask) -> tuple[SchedulePointKey, ...]:
    point = scheduled.schedule_point
    if point is None:
        return ()
    trade_date = shanghai_now(scheduled.scheduled_at).date().isoformat()
    strategies: tuple[str, ...]
    if point is SchedulePoint.TODAY_CHECKPOINT:
        strategies = ("today",)
    elif point in {SchedulePoint.AFTERNOON_CHECKPOINT, SchedulePoint.AFTERNOON_FREEZE}:
        strategies = scheduled.freeze_strategies
    elif point is SchedulePoint.TODAY_FREEZE:
        strategies = scheduled.freeze_strategies or ("today",)
    else:
        strategies = ("-",)
    return tuple(SchedulePointKey(trade_date, point, strategy) for strategy in strategies)


def _schedule_point_order(point: SchedulePoint) -> int:
    return tuple(item for item, _strategies in _schedule_point_strategies()).index(point)


def _schedule_retry_delay(attempt_count: int) -> float:
    values = (1.0, 2.0, 5.0, 10.0, 30.0)
    return values[min(max(0, attempt_count - 1), len(values) - 1)]


__all__ = [
    "CadenceBand",
    "CadenceBatch",
    "CadencePlanner",
    "CadencePlannerStatus",
    "CadencePolicy",
    "PERIODIC_TASKS",
    "PipelineTask",
    "SchedulePointKey",
    "SchedulePointLifecycle",
    "SchedulePointResult",
    "SchedulePointState",
    "ScheduledPipelineTask",
    "cadence_band",
    "freshness_level",
]
