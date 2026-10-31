"""Coordinate one deadline and one atomic publication across enabled profiles."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from trader.infra.sqlite_observation import SqliteActivity, SqliteObservation
from trader.recommendation.application.pipeline.freeze_publish.current_publisher import (
    PreparedCurrentPair,
    PublishCurrentBatch,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import (
    UnifiedPairedPublishResult,
    UnifiedProfiledPublishResult,
)
from trader.recommendation.application.runtime.cadence import ScheduledPipelineTask
from trader.recommendation.application.runtime.round_inputs import ProfileRoundInputs, RoundInputs

ROUND_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class RoundAdmission:
    sequence: int
    tasks: tuple[ScheduledPipelineTask, ...]
    started_monotonic: float
    deadline_monotonic: float


@dataclass(frozen=True)
class RecommendationRoundStatus:
    sequence: int
    profiles: tuple[str, ...]
    outcome: str
    finished: bool
    elapsed_ms: float
    cleanup_ms: float
    pending_profiles: tuple[str, ...]
    running_profiles: tuple[str, ...]
    staged_profiles: tuple[str, ...]
    database: SqliteActivity


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
        self._prepare_publication: dict[str, Callable[[], None]] = {}
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
        self._inputs: RoundInputs | None = None
        self._observation = SqliteObservation()
        self._round_profiles: tuple[str, ...] = ()
        self._outcome = "running"
        self._revoked_at: float | None = None
        self._last_status: RecommendationRoundStatus | None = None
        self._monotonic = monotonic
        self._round_timeout_seconds = round_timeout_seconds
        self._publish_batch = publish_batch
        self._publish_batch_event = publish_batch_event

    def register(self, profile: str, prepare_publication: Callable[[], None] | None = None) -> None:
        with self._condition:
            if self._active is not None or profile in self._profiles:
                raise ValueError("recommendation round profile registration is invalid")
            self._profiles.append(profile)
            if prepare_publication is not None:
                self._prepare_publication[profile] = prepare_publication

    def enter(self, profile: str, tasks: tuple[ScheduledPipelineTask, ...]) -> RoundAdmission | None:
        with self._condition:
            if profile not in self._profiles:
                return None
            self._expire_locked()
            self._close_finished_locked()
            if self._active is None:
                if not tasks:
                    return None
                for prepare in self._prepare_publication.values():
                    prepare()
                self._sequence += 1
                started = self._monotonic()
                self._active = RoundAdmission(
                    self._sequence,
                    tasks,
                    started,
                    started + self._round_timeout_seconds,
                )
                self._pending = set(self._profiles)
                self._round_profiles = tuple(self._profiles)
                self._observation = SqliteObservation(on_drained=self._workers_drained)
                self._outcome = "running"
                self._revoked_at = None
                self._cancelled = False
                self._inputs_pending = False
                self._staged.clear()
                self._publication = None
                sequence = self._sequence
                self._inputs = RoundInputs(tuple(self._profiles), lambda: self.remaining_seconds(sequence))
            if self._cancelled or profile not in self._pending:
                return None
            self._pending.remove(profile)
            self._collecting.add(profile)
            self._preparing.add(profile)
            self._running.add(profile)
            return self._active

    def observe(self, sequence: int) -> AbstractContextManager[None]:
        with self._condition:
            if self._active is None or self._active.sequence != sequence:
                raise ValueError("round_observation_identity_missing")
            return self._observation.bind()

    def status(self) -> RecommendationRoundStatus | None:
        with self._condition:
            # Observing status must not expire work or mutate scheduler state.
            return self._snapshot_locked() if self._active is not None else self._last_status

    def _snapshot_locked(self, *, finished: bool = False) -> RecommendationRoundStatus:
        admission = self._active
        if admission is None:
            raise RuntimeError("round_status_identity_missing")
        now = self._monotonic()
        return RecommendationRoundStatus(
            admission.sequence, self._round_profiles, self._outcome, finished,
            max(0.0, now - admission.started_monotonic) * 1000,
            max(0.0, now - self._revoked_at) * 1000 if self._revoked_at is not None else 0.0,
            tuple(sorted(self._pending)), tuple(sorted(self._running)), tuple(sorted(self._staged)),
            self._observation.snapshot(),
        )

    def inputs(self, profile: str, sequence: int | None) -> ProfileRoundInputs | None:
        with self._condition:
            if sequence is None:
                return None
            if self._active is None or self._active.sequence != sequence or self._inputs is None:
                raise ValueError("round_input_identity_missing")
            return ProfileRoundInputs(profile, self._inputs)

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
            self._cancel_locked("waiting_inputs")
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
            if (
                pair.profile != profile
                or profile not in self._running
                or profile in self._staged
                or not self._matches_locked(sequence)
            ):
                return UnifiedPairedPublishResult(False, "round_cancelled")
            self._staged[profile] = pair
            if len(self._staged) == len(self._profiles):
                self._commit_locked(sequence)
            self._condition.notify_all()
            while self._publication is None and self._matches_locked(sequence):
                remaining = self._remaining_locked()
                if remaining <= 0.0:
                    self._cancel_locked("timeout")
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
            self._cancel_locked("timeout")
            return
        pairs = tuple(self._staged[profile] for profile in self._profiles)
        publication = self._publish_batch(pairs, lambda: self._remaining_locked() > 0.0)
        self._publication = publication
        self._outcome = "published" if publication.accepted else publication.reason
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
                    self._cancel_locked("timeout")
                    return False
                self._condition.wait(remaining)
                if not self._matches_locked(sequence):
                    return False
            return True

    def finish(self, profile: str, sequence: int) -> None:
        with self._condition:
            if self._active is None or self._active.sequence != sequence:
                return
            if self._remaining_locked() <= 0.0:
                self._cancel_locked("timeout")
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
            self._prepare_publication.pop(profile, None)
            admitted = profile in self._pending or profile in self._running
            self._pending.discard(profile)
            if admitted:
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
            self._cancel_locked("timeout")
            self._close_finished_locked()

    def _cancel_locked(self, reason: str = "cancelled") -> None:
        if not self._cancelled and self._outcome != "published":
            self._outcome = reason
            self._revoked_at = self._monotonic()
        self._cancelled = True
        self._pending.clear()
        self._condition.notify_all()

    def _close_finished_locked(self) -> None:
        if not self._pending and not self._running and self._observation.snapshot().worker_inflight == 0:
            if self._active is not None:
                self._last_status = self._snapshot_locked(finished=True)
            self._active = None
            self._inputs = None
            self._collecting.clear()
            self._preparing.clear()
            self._staged.clear()
            self._publication = None
        self._condition.notify_all()

    def _workers_drained(self) -> None:
        with self._condition:
            self._close_finished_locked()


__all__ = ["ROUND_TIMEOUT_SECONDS", "RecommendationRounds", "RecommendationRoundStatus", "RoundAdmission"]
