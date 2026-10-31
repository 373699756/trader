"""Coordinate one deadline and one atomic publication across enabled profiles."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from trader.recommendation.application.pipeline.freeze_publish.current_publisher import (
    PreparedCurrentPair,
    PublishCurrentBatch,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import (
    UnifiedPairedPublishResult,
    UnifiedProfiledPublishResult,
)
from trader.recommendation.application.runtime.cadence import ScheduledPipelineTask

ROUND_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class RoundAdmission:
    sequence: int
    tasks: tuple[ScheduledPipelineTask, ...]
    started_monotonic: float
    deadline_monotonic: float


class RecommendationRounds:
    """Prevent overlap and expose only a complete, reader-atomic profile batch."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        round_timeout_seconds: float = ROUND_TIMEOUT_SECONDS,
        publish_batch: Callable[
            [tuple[PreparedCurrentPair, ...], Callable[[], bool]], UnifiedProfiledPublishResult
        ] = PublishCurrentBatch,
        publish_batch_event: Callable[[int, tuple], object] | None = None,
    ) -> None:
        if not math.isfinite(round_timeout_seconds) or not 0.0 < round_timeout_seconds <= ROUND_TIMEOUT_SECONDS:
            raise ValueError("recommendation round timeout must be within 120 seconds")
        self._condition = threading.Condition()
        self._profiles: list[str] = []
        self._sequence = 0
        self._active: RoundAdmission | None = None
        self._pending: set[str] = set()
        self._collecting: set[str] = set()
        self._preparing: set[str] = set()
        self._running: set[str] = set()
        self._staged: dict[str, PreparedCurrentPair] = {}
        self._publication: UnifiedProfiledPublishResult | None = None
        self._cancelled = False
        self._inputs_pending = False
        self._monotonic = monotonic
        self._round_timeout_seconds = round_timeout_seconds
        self._publish_batch = publish_batch
        self._publish_batch_event = publish_batch_event

    def register(self, profile: str) -> None:
        with self._condition:
            if self._active is not None or profile in self._profiles:
                raise ValueError("recommendation round profile registration is invalid")
            self._profiles.append(profile)

    def enter(self, profile: str, tasks: tuple[ScheduledPipelineTask, ...]) -> RoundAdmission | None:
        with self._condition:
            if profile not in self._profiles:
                return None
            self._expire_locked()
            if self._active is None:
                if not tasks:
                    return None
                self._sequence += 1
                started = self._monotonic()
                self._active = RoundAdmission(
                    self._sequence,
                    tasks,
                    started,
                    started + self._round_timeout_seconds,
                )
                self._pending = set(self._profiles)
                self._cancelled = False
                self._inputs_pending = False
                self._staged.clear()
                self._publication = None
            if self._cancelled or profile not in self._pending:
                return None
            self._pending.remove(profile)
            self._collecting.add(profile)
            self._preparing.add(profile)
            self._running.add(profile)
            return self._active

    def cancelled(self, sequence: int) -> bool:
        with self._condition:
            self._expire_locked()
            return self._active is None or self._active.sequence != sequence or self._cancelled

    def remaining_seconds(self, sequence: int) -> float:
        with self._condition:
            if self._active is None or self._active.sequence != sequence or self._cancelled:
                return 0.0
            return max(0.0, self._active.deadline_monotonic - self._monotonic())

    def defer(self, sequence: int) -> bool:
        """Cancel a pending-input attempt so every admitted preview can retry."""
        with self._condition:
            if self._active is None or self._active.sequence != sequence:
                return False
            if self._inputs_pending:
                return True
            if not self._matches_locked(sequence):
                return False
            self._inputs_pending = True
            self._cancel_locked()
            return True

    def inputs_pending(self, sequence: int) -> bool:
        with self._condition:
            return self._active is not None and self._active.sequence == sequence and self._inputs_pending

    def prepared(self, profile: str, sequence: int) -> bool:
        return self._wait_for_collection(profile, sequence, self._preparing)

    def collected(self, profile: str, sequence: int) -> bool:
        return self._wait_for_collection(profile, sequence, self._collecting)

    def stage(self, profile: str, sequence: int, pair: PreparedCurrentPair) -> UnifiedPairedPublishResult:
        with self._condition:
            if pair.profile != profile or not self._matches_locked(sequence):
                return UnifiedPairedPublishResult(False, "round_cancelled")
            self._staged[profile] = pair
            if len(self._staged) == len(self._profiles):
                self._commit_locked(sequence)
            self._condition.notify_all()
            while self._publication is None and self._matches_locked(sequence):
                remaining = self._remaining_locked()
                if remaining <= 0.0:
                    self._cancel_locked()
                    break
                self._condition.wait(remaining)
            publication = self._publication
            if publication is None or not publication.accepted:
                return UnifiedPairedPublishResult(
                    False, publication.reason if publication is not None else "round_timeout"
                )
            events = dict(publication.events).get(profile)
            if events is None:
                return UnifiedPairedPublishResult(False, "profile_publication_missing")
            return UnifiedPairedPublishResult(True, publication.reason, events)

    def _commit_locked(self, sequence: int) -> None:
        if not self._matches_locked(sequence) or self._remaining_locked() <= 0.0:
            self._cancel_locked()
            return
        pairs = tuple(self._staged[profile] for profile in self._profiles)
        publication = self._publish_batch(pairs, lambda: self._remaining_locked() > 0.0)
        self._publication = publication
        if publication.accepted and self._publish_batch_event is not None:
            self._publish_batch_event(sequence, publication.events)

    def _wait_for_collection(self, profile: str, sequence: int, remaining_profiles: set[str]) -> bool:
        with self._condition:
            if not self._matches_locked(sequence):
                return False
            remaining_profiles.discard(profile)
            self._condition.notify_all()
            while self._pending or remaining_profiles:
                remaining = self._remaining_locked()
                if remaining <= 0.0:
                    self._cancel_locked()
                    return False
                self._condition.wait(remaining)
                if not self._matches_locked(sequence):
                    return False
            return True

    def finish(self, profile: str, sequence: int) -> None:
        with self._condition:
            if self._active is None or self._active.sequence != sequence:
                return
            if profile in self._running and profile not in self._staged:
                self._cancel_locked()
            self._pending.discard(profile)
            self._collecting.discard(profile)
            self._preparing.discard(profile)
            self._running.discard(profile)
            self._close_finished_locked()

    def withdraw(self, profile: str) -> None:
        with self._condition:
            if profile in self._profiles:
                self._profiles.remove(profile)
            self._pending.discard(profile)
            if profile in self._running:
                self._cancel_locked()
            self._close_finished_locked()

    def _matches_locked(self, sequence: int) -> bool:
        self._expire_locked()
        return self._active is not None and self._active.sequence == sequence and not self._cancelled

    def _remaining_locked(self) -> float:
        if self._active is None:
            return 0.0
        return max(0.0, self._active.deadline_monotonic - self._monotonic())

    def _expire_locked(self) -> None:
        if self._active is not None and self._remaining_locked() <= 0.0:
            self._cancel_locked()
            self._close_finished_locked()

    def _cancel_locked(self) -> None:
        self._cancelled = True
        self._pending.clear()
        self._condition.notify_all()

    def _close_finished_locked(self) -> None:
        if not self._pending and not self._running:
            self._active = None
            self._staged.clear()
            self._publication = None
        self._condition.notify_all()


__all__ = ["ROUND_TIMEOUT_SECONDS", "RecommendationRounds", "RoundAdmission"]
