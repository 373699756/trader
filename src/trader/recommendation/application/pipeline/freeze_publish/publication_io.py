"""Bounded, immutable receipts for actual layer-14 I/O, outside decision hashes."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal

from trader.recommendation.domain.evidence.pipeline import (
    Severity,
    SourceHealth,
    SourceHealthState,
    StageReasonAggregate,
    StageState,
)
from trader.recommendation.domain.publication.decision_identity import ScoredDecision
from trader.recommendation.domain.publication.models import Strategy

PublicationOperation = Literal[
    "current_publish",
    "decision_event",
    "observer_enqueue",
    "checkpoint_write",
    "formal_lookup",
    "checkpoint_lookup",
    "freeze_seal",
    "formal_write",
    "formal_publish",
    "checkpoint_consume",
    "formal_restore",
]


@dataclass(frozen=True)
class PublicationIoTarget:
    strategy: Strategy
    trade_date: date
    decision_version: str | None
    sequence: int

    @classmethod
    def decision(cls, decision: ScoredDecision) -> PublicationIoTarget:
        return cls(decision.strategy, decision.trade_date, decision.version, decision.sequence)

    def __post_init__(self) -> None:
        if self.strategy not in {Strategy.TOMORROW, Strategy.D25}:
            raise ValueError("publication I/O requires a scored strategy")
        if self.sequence < 0 or (self.sequence > 0) != (self.decision_version is not None):
            raise ValueError("publication I/O target identity is invalid")


@dataclass(frozen=True)
class PublicationIoSnapshot:
    attempt_id: int
    operation: PublicationOperation
    target: PublicationIoTarget
    as_of: datetime
    state: StageState
    output_version: str | None
    input_count: int
    output_count: int
    rejected_count: int
    pending_count: int
    failed_count: int
    reasons: tuple[StageReasonAggregate, ...]
    source_health: SourceHealth
    latency_ms: int | None


class PublicationIoTracker:
    """One latest attempt per operation/strategy; late completions cannot replace it.

    Counts are operations (one decision/record per call), never selected stocks.
    A cold lookup may have no decision identity until the repository returns it.
    """

    def __init__(self, *, now: Callable[[], datetime], monotonic: Callable[[], float]) -> None:
        self._now = now
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._sequence = 0
        self._latest: dict[tuple[Strategy, PublicationOperation], PublicationIoSnapshot] = {}

    def begin(self, operation: PublicationOperation, target: PublicationIoTarget) -> tuple[int, float]:
        at, started = self._now(), self._monotonic()
        _require_time(at)
        with self._lock:
            self._sequence += 1
            ticket = self._sequence
            # A delayed older cycle must not become the visible operation owner.
            previous = self._latest.get((target.strategy, operation))
            lookup = operation in {"formal_lookup", "checkpoint_lookup"}
            if previous is None or (
                target.trade_date >= previous.target.trade_date
                if lookup
                else (target.trade_date, target.sequence) >= (previous.target.trade_date, previous.target.sequence)
            ):
                last_success = previous.source_health.latest_success_at if previous is not None else None
                self._latest[(target.strategy, operation)] = PublicationIoSnapshot(
                    ticket,
                    operation,
                    target,
                    at,
                    StageState.NOT_READY,
                    None,
                    1,
                    0,
                    0,
                    1,
                    0,
                    (),
                    SourceHealth(SourceHealthState.DEGRADED, 1, 0, last_success),
                    None,
                )
            return ticket, started

    def finish(
        self,
        ticket: int,
        started: float,
        operation: PublicationOperation,
        target: PublicationIoTarget,
        outcome: PublicationIoCompletion,
        *,
        error: str | None,
    ) -> None:
        at, elapsed = self._now(), max(0, round((self._monotonic() - started) * 1000))
        _require_time(at)
        state = StageState.FAILED if error is not None else outcome.state
        reason = error or outcome.reason
        source = SourceHealth(
            SourceHealthState.UNAVAILABLE if error else SourceHealthState.READY,
            1,
            0 if error else 1,
            None if error else at,
            None if error else 0.0,
        )
        receipt = PublicationIoSnapshot(
            ticket,
            operation,
            outcome.target or target,
            at,
            state,
            outcome.output_version,
            1,
            int(state is StageState.READY),
            0,
            int(state is StageState.NOT_READY),
            int(state is StageState.FAILED),
            (
                StageReasonAggregate(
                    reason, reason.replace("_", " "), 1, Severity.ERROR if state is StageState.FAILED else Severity.INFO
                ),
            ),
            source,
            elapsed,
        )
        with self._lock:
            current = self._latest.get((target.strategy, operation))
            if current is not None and current.attempt_id == ticket:
                if error is not None:
                    receipt = replace(
                        receipt,
                        source_health=replace(
                            receipt.source_health, latest_success_at=current.source_health.latest_success_at
                        ),
                    )
                self._latest[(target.strategy, operation)] = receipt

    def snapshots(self) -> tuple[PublicationIoSnapshot, ...]:
        try:
            at = self._now()
            _require_time(at)
        except (RuntimeError, TypeError, ValueError):
            # Preserve real receipts; an unavailable clock cannot invent freshness.
            at = None
        with self._lock:
            return tuple(
                replace(
                    item,
                    source_health=replace(
                        item.source_health,
                        age_seconds=max(0.0, (at - item.source_health.latest_success_at).total_seconds())
                        if at is not None and item.source_health.latest_success_at is not None
                        else None,
                    ),
                )
                for item in sorted(self._latest.values(), key=lambda item: item.attempt_id)
            )


@dataclass
class PublicationIoCompletion:
    """Private call-local completion builder; only immutable receipts escape."""

    state: StageState = StageState.READY
    reason: str = "completed"
    output_version: str | None = None
    target: PublicationIoTarget | None = None


@contextmanager
def observe_publication_io(
    tracker: PublicationIoTracker | None,
    operation: PublicationOperation,
    target: PublicationIoTarget,
) -> Iterator[PublicationIoCompletion]:
    outcome = PublicationIoCompletion()
    ticket: tuple[int, float] | None = None
    if tracker is not None:
        try:
            ticket = tracker.begin(operation, target)
        except (RuntimeError, TypeError, ValueError):
            # Telemetry must never revoke a valid business result.
            ticket = None
    error: str | None = None
    try:
        yield outcome
    except Exception as exc:
        error = f"io_{type(exc).__name__.lower()}"
        raise
    finally:
        if tracker is not None and ticket is not None:
            try:
                tracker.finish(*ticket, operation, target, outcome, error=error)
            except (RuntimeError, TypeError, ValueError):
                pass


def _require_time(at: datetime) -> None:
    if getattr(at.tzinfo, "key", None) != "Asia/Shanghai":
        raise ValueError("publication observation clock must use Asia/Shanghai")
