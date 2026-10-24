"""Resolve one process's live or ephemeral preview authority from prepared references."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from datetime import date, datetime

from trader.recommendation.application.ports.runtime import TradingCalendarUnavailableError
from trader.recommendation.application.runtime.schedule import ShanghaiNow
from trader.recommendation.domain.publication.preview import PreviewContext


class RecommendationRunScope:
    def __init__(
        self,
        enabled: bool,
        prepare: Callable[[], None],
        open_dates: Callable[[], tuple[date, ...]],
        now: Callable[[], datetime],
        new_run_id: Callable[[], str],
    ) -> None:
        self._prepare = prepare
        self._open_dates = open_dates
        self._now = now
        self._new_run_id = new_run_id
        self._lock = threading.RLock()
        self._resolved = not enabled
        self._live_selected = not enabled
        self._preview: PreviewContext | None = None
        self._pending_live: deque[Callable[[], object]] = deque()
        self._next_sequence = 1

    @property
    def resolved(self) -> bool:
        with self._lock:
            return self._resolved

    @property
    def is_preview(self) -> bool:
        with self._lock:
            return self._preview is not None

    @property
    def preview(self) -> PreviewContext | None:
        with self._lock:
            return self._preview

    def initialize(self) -> None:
        with self._lock:
            if self._resolved:
                return
            try:
                if not self._live_selected:
                    self._prepare()
                    calendar = self._open_dates()
                    observed_at = ShanghaiNow(self._now())
                    if (
                        not calendar
                        or any(type(day) is not date for day in calendar)
                        or calendar != tuple(sorted(set(calendar)))
                    ):
                        raise TradingCalendarUnavailableError("preview_calendar_invalid")
                    if observed_at.date() in calendar:
                        self._live_selected = True
                    else:
                        previous = max((day for day in calendar if day < observed_at.date()), default=None)
                        if previous is None:
                            raise TradingCalendarUnavailableError("preview_previous_session_unavailable")
                        self._preview = PreviewContext(self._new_run_id(), previous, observed_at)
                        self._pending_live.clear()
                        self._resolved = True
                        return
                while self._pending_live:
                    self._pending_live[0]()
                    self._pending_live.popleft()
            except (OSError, RuntimeError, ValueError):
                return
            self._resolved = True

    def run_live(self, operation: Callable[[], object]) -> None:
        with self._lock:
            if not self._resolved:
                self._pending_live.append(operation)
                return
            allowed = self._preview is None
        if allowed:
            operation()

    def next_sequence(self, allocate: Callable[[], int]) -> int:
        with self._lock:
            if not self._resolved:
                raise RuntimeError("recommendation_run_scope_unresolved")
            if self._preview is not None:
                sequence = self._next_sequence
                self._next_sequence += 2
                return sequence
        return allocate()


__all__ = ["RecommendationRunScope"]
