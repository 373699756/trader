"""Non-blocking daily-history warmup for the live recommendation path."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.application.ports.market import MarketDataDeadlineExceededError
from trader.application.source_lanes import SourceRequestSupersededError
from trader.infra.market_data.market_cache_identity import _normalize_codes, _source_batch_identity
from trader.infra.market_data.service_execution import MarketTaskRunner
from trader.infra.market_data.service_history import HistoryCache
from trader.infra.market_data.service_tushare import ReferenceLoader

_LOGGER = logging.getLogger(__name__)
_HISTORY_SOURCE_LANE = "history"
_PERMANENT_TUSHARE_DEGRADATIONS = frozenset({"missing_token", "insufficient_points", "permission_denied"})
_RETRY_DELAYS_SECONDS = (60.0, 120.0, 240.0, 480.0, 900.0)
_SOURCE_ATTEMPTS_PER_CODE = 4
_PERSISTENCE_RESERVE_RATIO = 0.1


@dataclass(frozen=True)
class HistoryWarmupStatus:
    planned_count: int
    completed_count: int
    failure_count: int
    inflight_count: int
    retry_deferred_count: int
    unique_failure_count: int
    next_retry_seconds: float | None
    last_source: str
    timeout_count: int
    inflight_age_seconds: float | None
    batch_timeout_seconds: float


@dataclass(frozen=True)
class HistoryWarmupPolicy:
    batch_size: int
    batch_timeout_seconds: float
    source_attempt_timeout_seconds: float


def build_history_warmup_policy(
    *,
    worker_count: int,
    source_timeout_seconds: float,
    maximum_batch_size: int,
    maximum_batch_timeout_seconds: float,
) -> HistoryWarmupPolicy:
    if min(worker_count, maximum_batch_size) < 1:
        raise ValueError("history warmup worker and batch limits must be positive")
    if min(source_timeout_seconds, maximum_batch_timeout_seconds) <= 0.0:
        raise ValueError("history warmup timeout limits must be positive")
    batch_size = min(worker_count, maximum_batch_size)
    route_budget = source_timeout_seconds * _SOURCE_ATTEMPTS_PER_CODE
    batch_timeout_seconds = min(
        maximum_batch_timeout_seconds,
        route_budget / (1.0 - _PERSISTENCE_RESERVE_RATIO),
    )
    persistence_reserve = batch_timeout_seconds * _PERSISTENCE_RESERVE_RATIO
    source_attempt_timeout_seconds = min(
        source_timeout_seconds,
        (batch_timeout_seconds - persistence_reserve) / _SOURCE_ATTEMPTS_PER_CODE,
    )
    return HistoryWarmupPolicy(
        batch_size=batch_size,
        batch_timeout_seconds=batch_timeout_seconds,
        source_attempt_timeout_seconds=source_attempt_timeout_seconds,
    )


class HistoryWarmupOptions(TypedDict):
    batch_size: int
    batch_timeout_seconds: float
    monotonic: Callable[[], float]


class HistoryWarmup:
    def __init__(
        self,
        history: HistoryCache,
        references: ReferenceLoader,
        runner: MarketTaskRunner,
        **options: Unpack[HistoryWarmupOptions],
    ) -> None:
        batch_timeout_seconds = options["batch_timeout_seconds"]
        if batch_timeout_seconds <= 0.0:
            raise ValueError("history warmup batch timeout must be positive")
        self._history = history
        self._references = references
        self._runner = runner
        self._batch_size = max(1, options["batch_size"])
        self._batch_timeout_seconds = float(batch_timeout_seconds)
        self._monotonic = options["monotonic"]
        self._lock = threading.Lock()
        self._universe: tuple[str, ...] = ()
        self._inflight: set[str] = set()
        self._retry_attempts: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}
        self._planned_count = 0
        self._completed_count = 0
        self._failure_count = 0
        self._timeout_count = 0
        self._last_source = ""
        self._batch_started_at: float | None = None

    def schedule_history_warmup(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> None:
        normalized = _normalize_codes(codes)
        lanes = self._runner.source_lanes
        if not normalized or lanes is None:
            return
        now = self._monotonic()
        entries = self._history.entries()
        with self._lock:
            normalized = _stable_slot_order(self._universe, normalized)
            self._universe = normalized
            active_codes = set(normalized)
            self._retry_attempts = {
                code: attempts for code, attempts in self._retry_attempts.items() if code in active_codes
            }
            self._retry_after = {code: retry_at for code, retry_at in self._retry_after.items() if code in active_codes}
            if self._inflight:
                return
            missing = tuple(
                code
                for code in normalized
                if self._retry_after.get(code, 0.0) <= now
                and ((entry := entries.get(code)) is None or entry.expires_at <= now)
            )
        if not missing:
            return

        try:
            local_seed_codes = self._history.available_seed_codes(missing)
        except Exception as exc:
            local_seed_codes = ()
            _LOGGER.warning("local history seed discovery degraded: %s", type(exc).__name__)
        tushare_health = self._references.health()
        use_tushare = (
            not local_seed_codes
            and tushare_health is not None
            and tushare_health.enabled
            and not tushare_health.circuit_open
            and tushare_health.degraded_reason not in _PERMANENT_TUSHARE_DEGRADATIONS
            and tushare_health.history_mode == "forward_adjusted"
        )
        source = "local_seed" if local_seed_codes else ("tushare" if use_tushare else "tencent")
        batch = (local_seed_codes or missing)[: self._batch_size]
        with self._lock:
            self._inflight.update(batch)
            self._planned_count += len(batch)
            self._last_source = source
            self._batch_started_at = now

        identity = _source_batch_identity("history_warmup", batch, observed_at, source=source)
        deadline = self._runner.wall_clock() + timedelta(seconds=self._batch_timeout_seconds)
        future: Future[object]
        if use_tushare:
            future = lanes.submit(
                "tushare",
                identity,
                observed_at,
                self._warm_tushare_history_batch,
                batch,
                observed_at,
                deadline,
            )
        else:
            future = lanes.submit(
                _HISTORY_SOURCE_LANE,
                identity,
                observed_at,
                self._history.load,
                batch,
                deadline=deadline,
            )
        future.add_done_callback(lambda completed: self._finish_history_warmup(batch, completed))

    def _warm_tushare_history_batch(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        deadline: datetime,
    ) -> None:
        self._runner.ensure_before_deadline(deadline)
        observations = self._references.load_history_batch(codes, observed_at, force=False)
        self._runner.ensure_before_deadline(deadline)
        self._references.apply_history(observations)

    def _finish_history_warmup(
        self,
        codes: Sequence[str],
        future: Future[object],
    ) -> None:
        superseded = False
        timed_out = False
        try:
            future.result()
        except SourceRequestSupersededError:
            superseded = True
        except MarketDataDeadlineExceededError:
            timed_out = True
            _LOGGER.warning("history warmup batch exceeded its deadline")
        except Exception as exc:
            _LOGGER.warning("history warmup batch degraded: %s", type(exc).__name__)
        now = self._monotonic()
        entries = self._history.entries()
        with self._lock:
            self._inflight.difference_update(codes)
            if not self._inflight:
                self._batch_started_at = None
            if timed_out:
                self._timeout_count += 1
            covered_codes = {
                code
                for code in codes
                if (entry := entries.get(code)) is not None and entry.expires_at > now and len(entry.bars) >= 20
            }
            self._completed_count += len(covered_codes)
            for code in covered_codes:
                self._retry_attempts.pop(code, None)
                self._retry_after.pop(code, None)
            if not superseded:
                failed_codes = tuple(code for code in codes if code not in covered_codes)
                self._failure_count += len(failed_codes)
                for code in failed_codes:
                    attempts = self._retry_attempts.get(code, 0) + 1
                    self._retry_attempts[code] = attempts
                    delay = _RETRY_DELAYS_SECONDS[min(attempts - 1, len(_RETRY_DELAYS_SECONDS) - 1)]
                    self._retry_after[code] = now + delay
            universe = self._universe
        self._history.update_coverage(universe)
        lanes = self._runner.source_lanes
        history_lane_pending = bool(lanes is not None and lanes.status().lanes[_HISTORY_SOURCE_LANE].pending)
        if (
            universe
            and lanes is not None
            and not history_lane_pending
            and not lanes.is_stopped("history")
            and not lanes.is_stopped("tushare")
        ):
            self.schedule_history_warmup(universe, self._runner.wall_clock())

    def status(self) -> HistoryWarmupStatus:
        now = self._monotonic()
        with self._lock:
            deferred = tuple(
                retry_at - now
                for code, retry_at in self._retry_after.items()
                if code in self._universe and retry_at > now
            )
            return HistoryWarmupStatus(
                planned_count=self._planned_count,
                completed_count=self._completed_count,
                failure_count=self._failure_count,
                inflight_count=len(self._inflight),
                retry_deferred_count=len(deferred),
                unique_failure_count=len(self._retry_attempts),
                next_retry_seconds=min(deferred) if deferred else None,
                last_source=self._last_source,
                timeout_count=self._timeout_count,
                inflight_age_seconds=(
                    max(0.0, now - self._batch_started_at) if self._batch_started_at is not None else None
                ),
                batch_timeout_seconds=self._batch_timeout_seconds,
            )


def _stable_slot_order(previous: Sequence[str], current: Sequence[str]) -> tuple[str, ...]:
    current_codes = set(current)
    retained = [code for code in previous if code in current_codes]
    retained_codes = set(retained)
    retained.extend(code for code in current if code not in retained_codes)
    return tuple(retained)


__all__ = [
    "HistoryWarmup",
    "HistoryWarmupPolicy",
    "HistoryWarmupStatus",
    "build_history_warmup_policy",
]
