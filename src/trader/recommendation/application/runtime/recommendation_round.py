"""Coordinate collection and publication waves across the enabled profiles."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from trader.recommendation.application.runtime.cadence import ScheduledPipelineTask


@dataclass(frozen=True)
class RoundAdmission:
    sequence: int
    tasks: tuple[ScheduledPipelineTask, ...]


class RecommendationRounds:
    """No new collection wave until every admitted profile has left the old one."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._condition = threading.Condition()
        self._profiles: set[str] = set()
        self._sequence = 0
        self._active: RoundAdmission | None = None
        self._pending: set[str] = set()
        self._collecting: set[str] = set()
        self._preparing: set[str] = set()
        self._running: set[str] = set()
        self._cancelled = False
        self._started = 0.0
        self._monotonic = monotonic

    def register(self, profile: str) -> None:
        with self._condition:
            if self._active is not None or profile in self._profiles:
                raise ValueError("recommendation round profile registration is invalid")
            self._profiles.add(profile)

    def enter(self, profile: str, tasks: tuple[ScheduledPipelineTask, ...]) -> RoundAdmission | None:
        with self._condition:
            if profile not in self._profiles:
                return None
            if self._active is not None and self._pending and self._monotonic() - self._started >= 60.0:
                self._cancelled = True
                self._pending.clear()
                self._close_finished_locked()
            if self._active is None:
                if not tasks:
                    return None
                self._sequence += 1
                self._active = RoundAdmission(self._sequence, tasks)
                self._pending = set(self._profiles)
                self._cancelled = False
                self._started = self._monotonic()
            if self._cancelled or profile not in self._pending:
                return None
            self._pending.remove(profile)
            self._collecting.add(profile)
            self._preparing.add(profile)
            self._running.add(profile)
            return self._active

    def cancelled(self, sequence: int) -> bool:
        with self._condition:
            return self._active is None or self._active.sequence != sequence or self._cancelled

    def prepared(self, profile: str, sequence: int, *, timeout_seconds: float) -> bool:
        return self._wait_for_collection(profile, sequence, self._preparing, timeout_seconds)

    def collected(self, profile: str, sequence: int, *, timeout_seconds: float) -> bool:
        return self._wait_for_collection(profile, sequence, self._collecting, timeout_seconds)

    def _wait_for_collection(
        self, profile: str, sequence: int, remaining_profiles: set[str], timeout_seconds: float,
    ) -> bool:
        deadline = self._monotonic() + timeout_seconds
        with self._condition:
            if self._active is None or self._active.sequence != sequence:
                return False
            remaining_profiles.discard(profile)
            self._condition.notify_all()
            while self._pending or remaining_profiles:
                if self._cancelled:
                    return False
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    self._cancelled = True
                    self._pending.clear()
                    self._condition.notify_all()
                    return False
                self._condition.wait(remaining)
            return not self._cancelled

    def finish(self, profile: str, sequence: int) -> None:
        with self._condition:
            if self._active is None or self._active.sequence != sequence:
                return
            self._pending.discard(profile)
            self._collecting.discard(profile)
            self._preparing.discard(profile)
            self._running.discard(profile)
            self._close_finished_locked()

    def withdraw(self, profile: str) -> None:
        """Stop admitting a profile; running work must still leave through finally."""
        with self._condition:
            self._profiles.discard(profile)
            self._pending.discard(profile)
            if profile in self._running:
                self._cancelled = True
            self._close_finished_locked()

    def _close_finished_locked(self) -> None:
        if not self._pending and not self._running:
            self._active = None
        self._condition.notify_all()
