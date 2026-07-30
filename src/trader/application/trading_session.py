"""Thread-safe trading-session, calendar and clock-discontinuity state."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import Enum

from trader.application.schedule import phase_at, shanghai_now

_CALENDAR_RETRY_SECONDS = (30.0, 60.0, 120.0, 300.0)


class CalendarState(str, Enum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    CACHED = "cached"
    UNAVAILABLE = "calendar_unavailable"


@dataclass(frozen=True)
class TradingSessionStatus:
    trade_date: str
    calendar_state: CalendarState
    is_trading_day: bool | None
    phase: str
    evaluated_at: datetime
    next_retry_at: datetime | None
    generation: int
    discontinuity_reason: str | None

    def __post_init__(self) -> None:
        date.fromisoformat(self.trade_date)
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("session evaluation time must be timezone-aware")
        if self.next_retry_at is not None and (
            self.next_retry_at.tzinfo is None or self.next_retry_at.utcoffset() is None
        ):
            raise ValueError("calendar retry time must be timezone-aware")
        if self.generation < 0:
            raise ValueError("session generation cannot be negative")

    def to_json(self) -> Mapping[str, object]:
        return {
            "trade_date": self.trade_date,
            "calendar_state": self.calendar_state.value,
            "is_trading_day": self.is_trading_day,
            "phase": self.phase,
            "evaluated_at": self.evaluated_at.isoformat(),
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at is not None else None,
            "generation": self.generation,
            "discontinuity_reason": self.discontinuity_reason,
        }


RotationHook = Callable[[TradingSessionStatus], None]
CalendarLookup = Callable[[date], bool]


class TradingSessionTracker:
    def __init__(self, started_at: datetime) -> None:
        local = shanghai_now(started_at)
        self._lock = threading.Lock()
        self._status = TradingSessionStatus(
            trade_date=local.date().isoformat(),
            calendar_state=CalendarState.UNKNOWN,
            is_trading_day=None,
            phase="closed",
            evaluated_at=local,
            next_retry_at=None,
            generation=0,
            discontinuity_reason=None,
        )
        self._calendar_failure_count = 0
        self._last_wall_at: datetime | None = None
        self._last_monotonic_seconds: float | None = None
        self._rotation_hooks: list[RotationHook] = []

    def status(self) -> TradingSessionStatus:
        with self._lock:
            return self._status

    def snapshot(self) -> Mapping[str, object]:
        return self.status().to_json()

    def add_rotation_hook(self, hook: RotationHook) -> None:
        with self._lock:
            self._rotation_hooks.append(hook)

    def refresh(
        self,
        at: datetime,
        calendar_lookup: CalendarLookup,
        *,
        cache_used: bool = False,
    ) -> TradingSessionStatus:
        local = shanghai_now(at)
        rotation: TradingSessionStatus | None = None
        rotation_hooks: tuple[RotationHook, ...] = ()
        with self._lock:
            generation = self._status.generation
            current = self._rotate_for_trade_date_locked(local)
            if current.generation != generation:
                rotation = current
                rotation_hooks = tuple(self._rotation_hooks)
            if (
                current.calendar_state is CalendarState.UNAVAILABLE
                and current.next_retry_at is not None
                and local < current.next_retry_at
            ):
                return current
        if rotation is not None:
            for hook in rotation_hooks:
                hook(rotation)
        try:
            is_trading_day = bool(calendar_lookup(local.date()))
        except (OSError, RuntimeError, ValueError):
            with self._lock:
                retry_index = min(self._calendar_failure_count, len(_CALENDAR_RETRY_SECONDS) - 1)
                self._calendar_failure_count += 1
                self._status = replace(
                    self._status,
                    calendar_state=CalendarState.UNAVAILABLE,
                    is_trading_day=None,
                    phase="closed",
                    evaluated_at=local,
                    next_retry_at=local + timedelta(seconds=_CALENDAR_RETRY_SECONDS[retry_index]),
                )
                return self._status
        with self._lock:
            self._calendar_failure_count = 0
            self._status = replace(
                self._status,
                calendar_state=CalendarState.CACHED if cache_used else CalendarState.AVAILABLE,
                is_trading_day=is_trading_day,
                phase=phase_at(local, is_trading_day=is_trading_day).value,
                evaluated_at=local,
                next_retry_at=None,
            )
            return self._status

    def observe_clock(
        self,
        wall_at: datetime,
        *,
        monotonic_seconds: float,
        planned_interval_seconds: float,
    ) -> TradingSessionStatus:
        if planned_interval_seconds <= 0.0:
            raise ValueError("planned scheduler interval must be positive")
        local = shanghai_now(wall_at)
        hooks: tuple[RotationHook, ...] = ()
        with self._lock:
            reason = self._clock_discontinuity_reason_locked(
                local,
                monotonic_seconds=monotonic_seconds,
                planned_interval_seconds=planned_interval_seconds,
            )
            self._last_wall_at = local
            self._last_monotonic_seconds = monotonic_seconds
            if reason is None:
                self._status = replace(self._status, evaluated_at=local)
                return self._status
            self._status = self._rotated_status_locked(local, reason)
            hooks = tuple(self._rotation_hooks)
            status = self._status
        for hook in hooks:
            hook(status)
        return status

    def rotate(self, at: datetime, *, reason: str) -> TradingSessionStatus:
        if not reason:
            raise ValueError("session rotation reason cannot be empty")
        local = shanghai_now(at)
        with self._lock:
            self._status = self._rotated_status_locked(local, reason)
            hooks = tuple(self._rotation_hooks)
            status = self._status
        for hook in hooks:
            hook(status)
        return status

    def accepts(self, generation: int, trade_date: str) -> bool:
        with self._lock:
            return self._status.generation == generation and self._status.trade_date == trade_date

    def _rotate_for_trade_date_locked(self, local: datetime) -> TradingSessionStatus:
        if local.date().isoformat() != self._status.trade_date:
            self._status = self._rotated_status_locked(local, "trade_date_changed")
        return self._status

    def _rotated_status_locked(self, local: datetime, reason: str) -> TradingSessionStatus:
        trade_date_changed = local.date().isoformat() != self._status.trade_date
        return TradingSessionStatus(
            trade_date=local.date().isoformat(),
            calendar_state=CalendarState.UNKNOWN if trade_date_changed else self._status.calendar_state,
            is_trading_day=None if trade_date_changed else self._status.is_trading_day,
            phase="closed" if trade_date_changed else self._status.phase,
            evaluated_at=local,
            next_retry_at=None if trade_date_changed else self._status.next_retry_at,
            generation=self._status.generation + 1,
            discontinuity_reason=reason,
        )

    def _clock_discontinuity_reason_locked(
        self,
        local: datetime,
        *,
        monotonic_seconds: float,
        planned_interval_seconds: float,
    ) -> str | None:
        if local.date().isoformat() != self._status.trade_date:
            return "trade_date_changed"
        if self._last_wall_at is None or self._last_monotonic_seconds is None:
            return None
        wall_delta = (local - self._last_wall_at).total_seconds()
        monotonic_delta = monotonic_seconds - self._last_monotonic_seconds
        if wall_delta < -1.0:
            return "wall_clock_rollback"
        if monotonic_delta < 0.0 or abs(wall_delta - monotonic_delta) > 5.0:
            return "wall_monotonic_discontinuity"
        if wall_delta > max(90.0, 3.0 * planned_interval_seconds):
            return "scheduler_gap"
        return None


__all__ = [
    "CalendarState",
    "TradingSessionStatus",
    "TradingSessionTracker",
]
