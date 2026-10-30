"""Stateful no-catch-up cadence planning for production pipeline tasks."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from enum import Enum
from types import MappingProxyType

from trader.recommendation.application.runtime.schedule import (
    FreezeAt,
    MarketPhase,
    PhaseAt,
    SchedulePoint,
    SecondsUntilNextScheduleBoundary,
    ShanghaiNow,
)
from trader.recommendation.application.runtime.trading_session import SessionLease


class CadenceBand(str, Enum):
    BEFORE_CLOSE = "before_close"
    AFTER_CLOSE = "after_close"
    CLOSED = "closed"
    # Only independent minute/industry acquisition uses these windows.
    WARMUP = "warmup"
    MORNING = "morning"
    AFTERNOON = "afternoon"
    FINAL_REVIEW = "final_review"


class PipelineTask(str, Enum):
    FULL_MARKET = "full_market"
    CANDIDATE_QUOTES = "candidate_quotes"
    TOPK_QUOTES = "topk_quotes"
    INTRADAY_TAIL = "intraday_tail"
    LONG_QUOTES = "long_quotes"
    SCORE = "score"
    INDUSTRY_HEAT = "industry_heat"
    FREEZE = "freeze"
    CLOSE_QUOTES = "close_quotes"


class SchedulePointStatus(str, Enum):
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
    status: SchedulePointStatus
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


def TaskExecutionBudgetSeconds(task: PipelineTask) -> float | None:
    return {
        PipelineTask.FULL_MARKET: 20.0,
        PipelineTask.CANDIDATE_QUOTES: 15.0,
        PipelineTask.TOPK_QUOTES: 2.0,
        PipelineTask.INTRADAY_TAIL: 3.0,
        PipelineTask.LONG_QUOTES: 3.0,
        PipelineTask.SCORE: 15.0,
        PipelineTask.INDUSTRY_HEAT: 20.0,
        PipelineTask.CLOSE_QUOTES: 180.0,
        PipelineTask.FREEZE: None,
    }[task]


PERIODIC_TASKS = (
    PipelineTask.FULL_MARKET,
    PipelineTask.CANDIDATE_QUOTES,
    PipelineTask.TOPK_QUOTES,
    PipelineTask.INTRADAY_TAIL,
    PipelineTask.LONG_QUOTES,
    PipelineTask.INDUSTRY_HEAT,
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
            if CadenceBand.AFTER_CLOSE in values and task not in {PipelineTask.TOPK_QUOTES, PipelineTask.LONG_QUOTES}:
                raise ValueError("only quote display tasks may run after close")
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
    session: SessionLease | None = None
    round_sequence: int | None = None
    round_tasks: tuple[ScheduledPipelineTask, ...] = ()


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
        self._pending_scores: set[tuple[str, CadenceBand, PipelineTask]] = set()
        self._started_at = ShanghaiNow(started_at) if started_at is not None else None
        self._point_states: dict[SchedulePointKey, SchedulePointState] = {}

    def plan(self, at: datetime, *, is_trading_day: bool) -> CadenceBatch:
        with self._lock:
            return self._plan_locked(at, is_trading_day=is_trading_day)

    def plan_score_after_input(
        self,
        at: datetime,
        *,
        is_trading_day: bool,
    ) -> ScheduledPipelineTask | None:
        local = ShanghaiNow(at)
        freeze = FreezeAt(local, is_trading_day=is_trading_day)
        if not freeze.should_score:
            return None
        phase = freeze.phase
        band = ResolveCadenceBand(phase)
        interval = self._policy.interval(PipelineTask.SCORE, band)
        if interval is None:
            return None
        with self._lock:
            key = (local.date().isoformat(), band, PipelineTask.SCORE)
            due = self._next_due.get(key)
            if due is not None and local < due:
                self._pending_scores.add(key)
                return None
            self._pending_scores.discard(key)
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
                            if state.status is SchedulePointStatus.COMPLETED
                        }
                    )
                ),
            )

    def schedule_point_status(
        self,
        trade_date: str,
        schedule_point: SchedulePoint,
        strategy: str = "-",
    ) -> SchedulePointStatus | None:
        with self._lock:
            state = self._point_states.get(SchedulePointKey(trade_date, schedule_point, strategy))
            return state.status if state is not None else None

    def record_submission(
        self,
        scheduled: ScheduledPipelineTask,
        *,
        accepted: bool,
        at: datetime,
    ) -> None:
        local = ShanghaiNow(at)
        with self._lock:
            for key in _scheduled_point_keys(scheduled):
                current = self._point_states.get(key)
                eligible = {
                    SchedulePointStatus.PENDING,
                    SchedulePointStatus.RETRY_WAIT,
                }
                if not accepted:
                    eligible.add(SchedulePointStatus.INFLIGHT)
                if current is None or current.status not in eligible:
                    continue
                self._point_states[key] = replace(
                    current,
                    status=(SchedulePointStatus.INFLIGHT if accepted else SchedulePointStatus.PENDING),
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
        local = ShanghaiNow(at)
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
                        status=SchedulePointStatus.COMPLETED,
                        updated_at=local,
                        next_retry_at=None,
                    )
                elif raw_result is SchedulePointResult.MISSED:
                    updated = replace(
                        current,
                        status=SchedulePointStatus.MISSED,
                        updated_at=local,
                        next_retry_at=None,
                    )
                else:
                    attempt_count = current.attempt_count + 1
                    delay = _schedule_retry_delay(attempt_count)
                    updated = replace(
                        current,
                        status=SchedulePointStatus.RETRY_WAIT,
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
        local = ShanghaiNow(at)
        key = SchedulePointKey(trade_date, schedule_point, strategy)
        with self._lock:
            current = self._point_states.get(key)
            if current is None:
                current = SchedulePointState(SchedulePointStatus.INFLIGHT, 0, local)
                self._point_states[key] = current
            if result is SchedulePointResult.COMPLETED:
                point_status = SchedulePointStatus.COMPLETED
                next_retry_at = None
                attempt_count = current.attempt_count
            elif result is SchedulePointResult.MISSED:
                point_status = SchedulePointStatus.MISSED
                next_retry_at = None
                attempt_count = current.attempt_count
            else:
                point_status = SchedulePointStatus.RETRY_WAIT
                attempt_count = current.attempt_count + 1
                next_retry_at = local + timedelta(seconds=_schedule_retry_delay(attempt_count))
            self._point_states[key] = SchedulePointState(
                point_status,
                attempt_count,
                local,
                next_retry_at,
            )

    def rotate_session(self, at: datetime, *, reason: str) -> None:
        if not reason:
            raise ValueError("cadence rotation reason cannot be empty")
        local = ShanghaiNow(at)
        with self._lock:
            for key, state in tuple(self._point_states.items()):
                if state.status not in {
                    SchedulePointStatus.PENDING,
                    SchedulePointStatus.INFLIGHT,
                    SchedulePointStatus.RETRY_WAIT,
                }:
                    continue
                if key.schedule_point in {SchedulePoint.AFTERNOON_FREEZE, SchedulePoint.CLOSE_QUOTES}:
                    if state.status is SchedulePointStatus.INFLIGHT:
                        self._point_states[key] = replace(
                            state, status=SchedulePointStatus.PENDING, updated_at=local, next_retry_at=None
                        )
                    continue
                self._point_states[key] = replace(
                    state,
                    status=SchedulePointStatus.MISSED,
                    updated_at=local,
                    next_retry_at=None,
                )
            self._next_due.clear()
            self._pending_scores.clear()

    def interval_for(self, task: PipelineTask, at: datetime, *, is_trading_day: bool) -> float | None:
        if not is_trading_day:
            return None
        local = ShanghaiNow(at)
        band = _task_band(task, local, PhaseAt(local, is_trading_day=True))
        return self._policy.interval(task, band) if band is not None else None

    def has_unfinished_afternoon_freeze(self, trade_date: str) -> bool:
        with self._lock:
            return self._afternoon_freeze_unfinished(trade_date)

    def _plan_locked(self, at: datetime, *, is_trading_day: bool) -> CadenceBatch:
        local = ShanghaiNow(at)
        if self._started_at is None:
            self._started_at = local
        trade_date = local.date().isoformat()
        phase = PhaseAt(local, is_trading_day=is_trading_day)
        band = ResolveCadenceBand(phase)
        self._discard_old_state(trade_date, band)
        if not is_trading_day or band is CadenceBand.CLOSED:
            return CadenceBatch((), SecondsUntilNextScheduleBoundary(local, maximum_seconds=30.0))

        self._synchronize_schedule_points(local)
        tasks: list[ScheduledPipelineTask] = []
        due_points = self._due_schedule_points(local)
        if self._afternoon_freeze_unfinished(trade_date):
            due_points = tuple(item for item in due_points if item[0] is not SchedulePoint.CLOSE_QUOTES)
        for point, strategies in due_points:
            for task in _point_tasks(point, local, phase, strategies=strategies):
                tasks.append(task)
        self._append_periodic_tasks(tasks, local, phase)
        self._append_pending_score(tasks, local, phase)

        next_delays = tuple(
            max(0.05, (due - local).total_seconds())
            for key, due in self._next_due.items()
            if key[0] == trade_date
            and key[1] is _task_band(key[2], local, phase)
            and due > local
            and (key[2] is not PipelineTask.SCORE or key in self._pending_scores)
        )
        retry_delays = tuple(
            max(0.05, (state.next_retry_at - local).total_seconds())
            for key, state in self._point_states.items()
            if key.trade_date == trade_date
            and state.status is SchedulePointStatus.RETRY_WAIT
            and state.next_retry_at is not None
            and state.next_retry_at > local
        )
        maximum = min((*next_delays, *retry_delays), default=30.0)
        delay = SecondsUntilNextScheduleBoundary(local, maximum_seconds=maximum)
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
                    continue
                point_status = _initial_point_status(
                    point,
                    local=local,
                    boundary=boundary,
                    started_at=started,
                )
                self._point_states[key] = SchedulePointState(point_status, 0, local)

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
                if state.status is SchedulePointStatus.PENDING:
                    due_strategies.append(strategy)
                elif (
                    state.status is SchedulePointStatus.RETRY_WAIT
                    and state.next_retry_at is not None
                    and local >= state.next_retry_at
                ):
                    due_strategies.append(strategy)
            if due_strategies:
                grouped.append((point, tuple(due_strategies)))
        return tuple(grouped)

    def _afternoon_freeze_unfinished(self, trade_date: str) -> bool:
        return any(
            key.trade_date == trade_date
            and key.schedule_point is SchedulePoint.AFTERNOON_FREEZE
            and state.status
            in {
                SchedulePointStatus.PENDING,
                SchedulePointStatus.INFLIGHT,
                SchedulePointStatus.RETRY_WAIT,
            }
            for key, state in self._point_states.items()
        )

    def _append_periodic_tasks(
        self,
        tasks: list[ScheduledPipelineTask],
        local: datetime,
        phase: MarketPhase,
    ) -> None:
        trade_date = local.date().isoformat()
        point_task_names = {item.task for item in tasks}
        for task in PERIODIC_TASKS:
            band = _task_band(task, local, phase)
            if band is None:
                continue
            interval = self._policy.interval(task, band)
            if interval is None:
                continue
            key = (trade_date, band, task)
            due = self._next_due.get(key)
            if due is not None and local < due:
                continue
            if task not in point_task_names:
                tasks.append(ScheduledPipelineTask(task, local, phase))
            self._next_due[key] = local + timedelta(seconds=interval)

    def _append_pending_score(
        self,
        tasks: list[ScheduledPipelineTask],
        local: datetime,
        phase: MarketPhase,
    ) -> None:
        key = (local.date().isoformat(), ResolveCadenceBand(phase), PipelineTask.SCORE)
        due = self._next_due.get(key)
        if key not in self._pending_scores or due is None or local < due:
            return
        self._pending_scores.discard(key)
        if not FreezeAt(local, is_trading_day=True).should_score:
            return
        tasks.append(ScheduledPipelineTask(PipelineTask.SCORE, local, phase))
        interval = self._policy.interval(PipelineTask.SCORE, key[1])
        if interval is not None:
            self._next_due[key] = local + timedelta(seconds=interval)

    def _discard_old_state(self, trade_date: str, band: CadenceBand) -> None:
        self._next_due = {key: due for key, due in self._next_due.items() if key[0] == trade_date}
        self._pending_scores = {key for key in self._pending_scores if key[0] == trade_date and key[1] is band}


def ResolveCadenceBand(phase: MarketPhase) -> CadenceBand:
    return CadenceBand(phase.value)


def _task_band(task: PipelineTask, local: datetime, phase: MarketPhase) -> CadenceBand | None:
    if phase is not MarketPhase.BEFORE_CLOSE or task not in {PipelineTask.INTRADAY_TAIL, PipelineTask.INDUSTRY_HEAT}:
        return ResolveCadenceBand(phase)
    current = local.time().replace(tzinfo=None)
    if time(13, 0) <= current < time(14, 20):
        return CadenceBand.AFTERNOON
    if time(14, 20) <= current < time(14, 48):
        return CadenceBand.FINAL_REVIEW
    if task is PipelineTask.INDUSTRY_HEAT:
        if time(9, 15) <= current < time(9, 30):
            return CadenceBand.WARMUP
        if time(9, 30) <= current < time(11, 20):
            return CadenceBand.MORNING
    return None


def FreshnessLevel(age_seconds: float | None, interval_seconds: float | None) -> str:
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
        (SchedulePoint.AFTERNOON_FREEZE, ("tomorrow", "d25")),
        (SchedulePoint.CLOSE_QUOTES, ("-",)),
    )


def _point_boundary(local: datetime, point: SchedulePoint) -> datetime:
    raw = {
        SchedulePoint.AFTERNOON_FREEZE: time(15, 0),
        SchedulePoint.CLOSE_QUOTES: time(15, 0),
    }[point]
    return local.replace(hour=raw.hour, minute=raw.minute, second=raw.second, microsecond=0)


def _initial_point_status(
    point: SchedulePoint,
    *,
    local: datetime,
    boundary: datetime,
    started_at: datetime,
) -> SchedulePointStatus:
    point_status = SchedulePointStatus.MISSED
    if local < boundary:
        point_status = SchedulePointStatus.PENDING
    else:
        started_before_boundary = started_at.date() < boundary.date() or started_at < boundary
        eligible = point is not SchedulePoint.AFTERNOON_FREEZE or started_before_boundary
        if eligible:
            point_status = SchedulePointStatus.PENDING
    return point_status


def _point_tasks(
    point: SchedulePoint,
    at: datetime,
    phase: MarketPhase,
    *,
    strategies: tuple[str, ...],
) -> tuple[ScheduledPipelineTask, ...]:
    tasks: tuple[ScheduledPipelineTask, ...]
    if point is SchedulePoint.AFTERNOON_FREEZE:
        tasks = (ScheduledPipelineTask(PipelineTask.FREEZE, at, phase, strategies, point),)
    elif point is SchedulePoint.CLOSE_QUOTES:
        tasks = (ScheduledPipelineTask(PipelineTask.CLOSE_QUOTES, at, phase, (), point),)
    else:
        raise ValueError(f"unsupported schedule point: {point.value}")
    return tasks


def _scheduled_point_keys(scheduled: ScheduledPipelineTask) -> tuple[SchedulePointKey, ...]:
    point = scheduled.schedule_point
    if point is None:
        return ()
    trade_date = ShanghaiNow(scheduled.scheduled_at).date().isoformat()
    strategies: tuple[str, ...]
    if point is SchedulePoint.AFTERNOON_FREEZE:
        strategies = scheduled.freeze_strategies
    else:
        strategies = ("-",)
    return tuple(SchedulePointKey(trade_date, point, strategy) for strategy in strategies)


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
    "SchedulePointStatus",
    "SchedulePointResult",
    "SchedulePointState",
    "ScheduledPipelineTask",
    "ResolveCadenceBand",
    "FreshnessLevel",
]
