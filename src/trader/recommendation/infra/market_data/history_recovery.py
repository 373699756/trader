"""Bounded, non-persistent recovery for missing recommendation history."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, TimeoutError as FutureTimeoutError, as_completed
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from typing import Callable, Protocol

from trader.infra.market_data.history.history import DailyBar, PriceAdjustment
from trader.recommendation.application.runtime.workers import (
    BorrowExecutorOptions,
    BoundedExecutor,
    borrow_executor,
    submit_or_run_inline,
)


class HistorySource(Protocol):
    def fetch_history(self, code: str, *, days: int = 90) -> Sequence[DailyBar]: ...


@dataclass(frozen=True, slots=True)
class HistoryRecoveryStatus:
    planned_count: int
    success_count: int
    failure_count: int
    timeout_count: int
    last_source: str | None
    last_error: str | None
    requested_count: int = 0
    cache_hit_count: int = 0
    dispatched_count: int = 0
    deferred_count: int = 0
    inflight_count: int = 0
    latency_ms: int = 0


class HistoryRecovery:
    """Recover only missing bars for the active feature build.

    Results are deliberately returned to the caller and never persisted. The
    archive remains the only historical fact owner; this component only
    prevents an unavailable archive from making the current feature batch
    impossible to build.
    """

    def __init__(
        self,
        primary: HistorySource,
        fallback: HistorySource,
        *,
        worker_pool: BoundedExecutor | None,
        workers: int,
        minimum_rows: int = 20,
        batch_timeout_seconds: float = 12.0,
        max_batch_size: int = 120,
        ttl_seconds: float = 900.0,
        wall_clock: Callable[[], datetime] = datetime.now,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if workers < 1 or minimum_rows < 1 or batch_timeout_seconds <= 0.0 or max_batch_size < 1 or ttl_seconds <= 0.0:
            raise ValueError("history recovery limits must be positive")
        self._primary = primary
        self._fallback = fallback
        self._worker_pool = worker_pool
        self._workers = workers
        self._minimum_rows = minimum_rows
        self._batch_timeout_seconds = batch_timeout_seconds
        self._max_batch_size = max_batch_size
        self._ttl_seconds = ttl_seconds
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._lock = threading.Lock()
        self._status = HistoryRecoveryStatus(0, 0, 0, 0, None, None)
        self._recent: dict[str, tuple[tuple[DailyBar, ...], float]] = {}
        self._attempt_order: dict[str, int] = {}
        self._attempt_sequence = 0
        self._inflight: set[str] = set()

    def recover(
        self,
        codes: Sequence[str],
        *,
        days: int,
        deadline: datetime | None,
    ) -> Mapping[str, tuple[DailyBar, ...]]:
        requested = tuple(dict.fromkeys(code for code in codes if code.strip()))
        started = self._monotonic_clock()
        budget = self._batch_timeout_seconds
        if deadline is not None:
            budget = min(budget, max(0.0, (deadline - self._wall_clock()).total_seconds()))
        monotonic_deadline = started + budget
        with self._lock:
            self._recent = {code: item for code, item in self._recent.items() if item[1] > started}
            results = {code: self._recent[code][0] for code in requested if code in self._recent}
            missing = tuple(code for code in requested if code not in results)
            # Only dispatched work advances the cursor. Failed codes move to
            # the tail too, while unstarted waves retain their priority.
            selected = tuple(
                sorted(
                    (code for code in missing if code not in self._inflight),
                    key=lambda code: (self._attempt_order.get(code, 0), code),
                )[: self._max_batch_size]
            )
            self._inflight.update(selected)
        cache_hits = len(results)
        failures = 0
        timeouts = 0
        successful_recoveries = 0
        last_source: str | None = None
        last_error: str | None = None
        dispatched: dict[Future[tuple[str, tuple[DailyBar, ...], str]], str] = {}
        try:
            if selected and self._remaining_seconds(monotonic_deadline) > 0.0:
                with borrow_executor(
                    self._worker_pool,
                    BorrowExecutorOptions(
                        worker_count=min(self._workers, len(selected)),
                        queue_capacity=len(selected),
                        thread_name_prefix="history-recovery",
                        nested_inline=True,
                        wait_on_exit=False,
                    ),
                ) as pool:
                    for offset in range(0, len(selected), self._workers):
                        if self._remaining_seconds(monotonic_deadline) <= 0.0:
                            last_error = "history_recovery_deadline_exceeded"
                            break
                        wave = selected[offset : offset + self._workers]
                        futures: dict[Future[tuple[str, tuple[DailyBar, ...], str]], str] = {}
                        for code in wave:
                            if self._remaining_seconds(monotonic_deadline) <= 0.0:
                                break
                            with self._lock:
                                self._attempt_sequence += 1
                                self._attempt_order[code] = self._attempt_sequence
                            future = submit_or_run_inline(pool, self._recover_one, code, days, monotonic_deadline)
                            futures[future] = code
                            dispatched[future] = code
                        completed: set[Future[tuple[str, tuple[DailyBar, ...], str]]] = set()
                        try:
                            for future in as_completed(futures, timeout=self._remaining_seconds(monotonic_deadline)):
                                completed.add(future)
                                code = futures[future]
                                try:
                                    if self._remaining_seconds(monotonic_deadline) <= 0.0:
                                        raise TimeoutError("history recovery deadline exceeded")
                                    recovered_code, bars, source = future.result()
                                except TimeoutError:
                                    failures += 1
                                    timeouts += 1
                                    last_error = "history_recovery_deadline_exceeded"
                                    continue
                                except Exception as exc:  # vendor boundary
                                    failures += 1
                                    last_error = type(exc).__name__
                                    continue
                                if recovered_code == code and bars:
                                    results[code] = bars
                                    successful_recoveries += 1
                                    last_source = source
                                else:
                                    failures += 1
                                    last_error = "history_no_usable_qfq_rows"
                        except FutureTimeoutError:
                            pending = tuple(future for future in futures if future not in completed)
                            timeouts += len(pending)
                            failures += len(pending)
                            for future in pending:
                                future.cancel()
                            last_error = "history_recovery_deadline_exceeded"
                            break
            elif missing and budget <= 0.0:
                last_error = "history_recovery_deadline_exceeded"
        finally:
            with self._lock:
                expires_at = self._monotonic_clock() + self._ttl_seconds
                self._recent.update({code: (bars, expires_at) for code, bars in results.items() if code in selected})
                pending_futures = tuple((future, code) for future, code in dispatched.items() if not future.done())
                pending_codes = {code for _, code in pending_futures}
                self._inflight.difference_update(code for code in selected if code not in pending_codes)
                self._status = HistoryRecoveryStatus(
                    planned_count=len(selected),
                    success_count=successful_recoveries,
                    failure_count=failures,
                    timeout_count=timeouts,
                    last_source=last_source,
                    last_error=last_error,
                    requested_count=len(requested),
                    cache_hit_count=cache_hits,
                    dispatched_count=len(dispatched),
                    deferred_count=len(missing) - len(dispatched),
                    inflight_count=len(self._inflight),
                    latency_ms=max(0, int((self._monotonic_clock() - started) * 1000)),
                )
            # Late completions only release reservations. They cannot publish
            # stale bars into this or a subsequent feature batch.
            for future, code in pending_futures:
                future.add_done_callback(partial(self._release_inflight, code=code))
        return results

    def status(self) -> HistoryRecoveryStatus:
        with self._lock:
            return replace(self._status, inflight_count=len(self._inflight))

    def _release_inflight(self, _future: Future[tuple[str, tuple[DailyBar, ...], str]], *, code: str) -> None:
        with self._lock:
            self._inflight.discard(code)

    def _recover_one(self, code: str, days: int, deadline: float) -> tuple[str, tuple[DailyBar, ...], str]:
        if self._remaining_seconds(deadline) <= 0.0:
            raise TimeoutError("history recovery deadline exceeded")
        try:
            primary = self._valid_qfq(self._primary.fetch_history(code, days=days))
        except Exception:
            primary = ()
        if len(primary) >= self._minimum_rows:
            return code, primary[-days:], "primary"
        if self._remaining_seconds(deadline) <= 0.0:
            raise TimeoutError("history recovery deadline exceeded")
        try:
            fallback = self._valid_qfq(self._fallback.fetch_history(code, days=days))
        except Exception:
            fallback = ()
        selected = fallback if len(fallback) >= len(primary) else primary
        if len(selected) < self._minimum_rows:
            raise ValueError("history recovery returned too few qfq rows")
        return code, selected[-days:], "fallback"

    @staticmethod
    def _valid_qfq(bars: Sequence[DailyBar]) -> tuple[DailyBar, ...]:
        ordered = tuple(sorted(bars, key=lambda item: item.trade_date))
        if any(item.adjustment is not PriceAdjustment.QFQ for item in ordered):
            return ()
        return ordered

    def _remaining_seconds(self, deadline: float) -> float:
        return max(0.0, deadline - self._monotonic_clock())


__all__ = ["HistoryRecovery", "HistoryRecoveryStatus", "HistorySource"]
