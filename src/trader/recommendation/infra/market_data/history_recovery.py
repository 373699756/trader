"""Bounded, non-persistent recovery for missing recommendation history."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, TimeoutError as FutureTimeoutError, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    ) -> None:
        if (
            workers < 1
            or minimum_rows < 1
            or batch_timeout_seconds <= 0.0
            or max_batch_size < 1
            or ttl_seconds <= 0.0
        ):
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
        self._lock = threading.Lock()
        self._status = HistoryRecoveryStatus(0, 0, 0, 0, None, None)
        self._recent: dict[str, tuple[tuple[DailyBar, ...], datetime]] = {}

    def recover(
        self,
        codes: Sequence[str],
        *,
        days: int,
        deadline: datetime | None,
    ) -> Mapping[str, tuple[DailyBar, ...]]:
        requested = tuple(dict.fromkeys(code for code in codes if code.strip()))
        if not requested:
            return {}
        now = self._wall_clock()
        with self._lock:
            self._recent = {
                code: item for code, item in self._recent.items() if item[1] > now
            }
            results = {code: self._recent[code][0] for code in requested if code in self._recent}
        missing = tuple(code for code in requested if code not in results)
        selected = missing[: self._max_batch_size]
        failures = 0
        timeouts = 0
        successful_recoveries = 0
        last_source: str | None = None
        last_error: str | None = None
        effective_deadline = deadline or (self._wall_clock() + timedelta(seconds=self._batch_timeout_seconds))
        with borrow_executor(
            self._worker_pool,
            BorrowExecutorOptions(
                worker_count=min(self._workers, len(requested)),
                queue_capacity=len(requested),
                thread_name_prefix="history-recovery",
                nested_inline=True,
                wait_on_exit=False,
            ),
        ) as pool:
            for offset in range(0, len(selected), self._workers):
                wave = selected[offset : offset + self._workers]
                futures: dict[Future[tuple[str, tuple[DailyBar, ...], str]], str] = {
                    (future := submit_or_run_inline(pool, self._recover_one, code, days)): code for code in wave
                }
                try:
                    for future in as_completed(futures, timeout=self._remaining_seconds(effective_deadline)):
                        code = futures[future]
                        try:
                            recovered_code, bars, source = future.result()
                        except Exception as exc:  # vendor adapters classify the failure at this boundary
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
                    pending = tuple(future for future in futures if not future.done())
                    timeouts += len(pending)
                    failures += len(pending)
                    for future in pending:
                        future.cancel()
                    last_error = "history_recovery_deadline_exceeded"
                    break

        with self._lock:
            expires_at = self._wall_clock() + timedelta(seconds=self._ttl_seconds)
            self._recent.update({code: (bars, expires_at) for code, bars in results.items() if code in selected})
            self._status = HistoryRecoveryStatus(
                planned_count=len(selected),
                success_count=successful_recoveries,
                failure_count=failures,
                timeout_count=timeouts,
                last_source=last_source,
                last_error=last_error,
            )
        return results

    def status(self) -> HistoryRecoveryStatus:
        with self._lock:
            return self._status

    def _recover_one(self, code: str, days: int) -> tuple[str, tuple[DailyBar, ...], str]:
        try:
            primary = self._valid_qfq(self._primary.fetch_history(code, days=days))
        except Exception:
            primary = ()
        if len(primary) >= self._minimum_rows:
            return code, primary[-days:], "primary"
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

    def _remaining_seconds(self, deadline: datetime | None) -> float:
        if deadline is None:
            return self._batch_timeout_seconds
        return max(0.0, min(self._batch_timeout_seconds, (deadline - self._wall_clock()).total_seconds()))


__all__ = ["HistoryRecovery", "HistoryRecoveryStatus", "HistorySource"]
