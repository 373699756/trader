"""Read-only visibility for research identities published by recommendation cycles."""

from __future__ import annotations

import threading
from datetime import date

from trader.infra.shutdown import ShutdownDeadline, ShutdownStep
from trader.recommendation.application.ports.runtime import CycleRequest, ResearchIntent, ResearchRuntimeStatus
from trader.recommendation.domain.publication.models import Strategy


class ResearchRuntime:
    """Track published research identities without owning external I/O or scheduling."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._running = False
        self._trade_date: date | None = None
        self._intents: dict[Strategy, ResearchIntent] = {}
        self._membership: set[str] = set()

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            return True

    def stop(
        self,
        *,
        wait: bool,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep:
        del wait, deadline
        with self._lock:
            self._running = False
        return ShutdownStep("research", completed=True, timed_out=False)

    def observe(self, intent: ResearchIntent, request: CycleRequest) -> bool:
        if intent.strategy is not request.strategy or intent.trade_date != request.trade_date:
            raise ValueError("research intent must match its published freeze cycle")
        with self._lock:
            self._rotate_locked(intent.trade_date)
            self._intents[intent.strategy] = intent
            self._membership = {
                code for current_intent in self._intents.values() for code in current_intent.priority_codes
            }
        return False

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        del timeout_seconds
        return True

    def status(self) -> ResearchRuntimeStatus:
        with self._lock:
            return ResearchRuntimeStatus(
                state="idle" if self._running else "stopped",
                trade_date=self._trade_date.isoformat() if self._trade_date is not None else None,
                tracked_strategies=len(self._intents),
                tracked_output_codes=len(self._membership),
            )

    def _rotate_locked(self, trade_date: date) -> None:
        if self._trade_date == trade_date:
            return
        self._trade_date = trade_date
        self._intents.clear()
        self._membership.clear()


__all__ = ["ResearchRuntime"]
