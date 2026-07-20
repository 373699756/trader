"""Daily history cache and bounded loading operations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import as_completed, wait
from datetime import datetime
from typing import ParamSpec, TypeVar

from trader.application.ports import MarketDataDeadlineExceeded
from trader.application.workers import borrow_executor
from trader.infrastructure.market_data.history import DailyBar
from trader.infrastructure.market_data.service_models import _HistoryEntry
from trader.infrastructure.market_data.service_state import MarketServiceState
from trader.infrastructure.market_data.service_support import _history_version

_P = ParamSpec("_P")
_T = TypeVar("_T")


class MarketHistoryMixin(MarketServiceState):
    def _load_histories(
        self,
        codes: Sequence[str],
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Mapping[str, tuple[DailyBar, ...]]:
        result = {} if force else self._cached_histories(codes)
        with self._lock:
            previous = {code: self._history[code] for code in codes if code in self._history}
        missing = [code for code in codes if force or code not in result]
        if not missing:
            return result
        with borrow_executor(
            self._worker_pool,
            worker_count=min(self._history_workers, len(missing)),
            thread_name_prefix="candidate-history",
            queue_capacity=len(missing),
            wait_on_exit=deadline is None,
        ) as pool:
            futures = {}
            for code in missing:
                future = pool.submit(self._history_client.fetch_history, code, days=90)
                if future is None:
                    raise RuntimeError("data worker queue rejected history task")
                futures[future] = code
            if deadline is None:
                completed = as_completed(futures)
            else:
                timeout = max(0.0, (deadline - self._wall_clock()).total_seconds())
                completed_set, pending = wait(futures, timeout=timeout)
                if pending:
                    for future in pending:
                        future.cancel()
                    raise MarketDataDeadlineExceeded("history preload exceeded its batch deadline")
                completed = iter(completed_set)
            pending_entries: dict[str, _HistoryEntry] = {}
            for future in completed:
                code = futures[future]
                old_entry = previous.get(code)
                used_fallback = False
                try:
                    bars = tuple(future.result())
                except Exception:
                    bars = ()
                    with self._lock:
                        self._history_error_count += 1
                if bars and old_entry is not None and _history_version(bars) < _history_version(old_entry.bars):
                    bars = old_entry.bars
                    used_fallback = True
                    with self._lock:
                        self._history_out_of_order_count += 1
                elif not bars and old_entry is not None and old_entry.bars:
                    bars = old_entry.bars
                    used_fallback = True
                result[code] = bars
                pending_entries[code] = _HistoryEntry(
                    bars=bars,
                    expires_at=self._monotonic()
                    + (
                        min(60.0, self._history_ttl_seconds) if used_fallback or not bars else self._history_ttl_seconds
                    ),
                )
            self._ensure_before_deadline(deadline)
            with self._lock:
                self._ensure_before_deadline(deadline)
                self._history.update(pending_entries)
        return result

    def _cached_histories(self, codes: Iterable[str]) -> dict[str, tuple[DailyBar, ...]]:
        requested = tuple(codes)
        now = self._monotonic()
        result: dict[str, tuple[DailyBar, ...]] = {}
        with self._lock:
            for code in requested:
                entry = self._history.get(code)
                if entry is None:
                    continue
                if entry.expires_at <= now:
                    continue
                result[code] = entry.bars
        return result
