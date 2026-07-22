"""Shared bounded execution and deadline helpers for market-data services."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime
from typing import ParamSpec, TypeVar

from trader.application.cache import CacheIdentity, build_cache_identity
from trader.application.ports import MarketDataDeadlineExceeded
from trader.application.schedule import phase_at, shanghai_now
from trader.application.source_lanes import SourceLaneRegistry
from trader.application.workers import BoundedExecutor
from trader.infrastructure.market_data.service_models import _HistoryEntry

_P = ParamSpec("_P")
_T = TypeVar("_T")


class MarketExecutionMixin:
    _worker_pool: BoundedExecutor | None
    _source_lanes: SourceLaneRegistry | None
    _source_contract_versions: dict[str, str]
    _config_version: str
    _schema_version: str
    _history: dict[str, _HistoryEntry]
    _history_cache_limit: int
    _wall_clock: Callable[[], datetime]

    def _run_data_task(
        self,
        urgent: bool,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        pool = self._worker_pool
        if pool is None or not pool.is_running() or pool.owns_current_thread():
            return function(*args, **kwargs)
        submit = pool.submit_urgent if urgent else pool.submit
        future = submit(function, *args, **kwargs)
        if future is None:
            raise RuntimeError("data worker queue rejected source task")
        return future.result()

    def _run_source_task(
        self,
        source: str,
        identity: str,
        observed_at: datetime,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        lanes = self._source_lanes
        if lanes is None or lanes.owns_current_thread(source):
            return function(*args, **kwargs)
        return lanes.submit(source, identity, observed_at, function, *args, **kwargs).result()

    def _data_cache_identity(
        self,
        dataset: str,
        source: str,
        subject_key: str,
        request: Mapping[str, object],
        observed_at: datetime,
    ) -> CacheIdentity:
        local = shanghai_now(observed_at)
        return build_cache_identity(
            dataset=dataset,
            source=source,
            subject_key=subject_key,
            request=request,
            trade_date=local.date().isoformat(),
            phase=phase_at(local, is_trading_day=True).value,
            source_contract_version=self._source_contract_versions.get(source, f"{source}-component-v1"),
            config_version=self._config_version,
            schema_version=self._schema_version,
        )

    def _trim_history_fallback_locked(self, requested: set[str]) -> None:
        excess = len(self._history) - self._history_cache_limit
        if excess <= 0:
            return
        victims = sorted(
            self._history,
            key=lambda code: (code in requested, self._history[code].expires_at, code),
        )[:excess]
        for code in victims:
            self._history.pop(code, None)

    def _run_data_task_until(
        self,
        deadline: datetime | None,
        urgent: bool,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        if deadline is None:
            if self._source_lanes is not None:
                return function(*args, **kwargs)
            return self._run_data_task(urgent, function, *args, **kwargs)
        self._ensure_before_deadline(deadline)
        if self._source_lanes is not None:
            result = function(*args, **kwargs)
            self._ensure_before_deadline(deadline)
            return result
        pool = self._worker_pool
        if pool is None or not pool.is_running() or pool.owns_current_thread():
            result = function(*args, **kwargs)
            self._ensure_before_deadline(deadline)
            return result
        submit = pool.submit_urgent if urgent else pool.submit
        future = submit(function, *args, **kwargs)
        if future is None:
            raise RuntimeError("data worker queue rejected deadline-bound source task")
        remaining = max(0.0, (deadline - self._wall_clock()).total_seconds())
        try:
            result = future.result(timeout=remaining)
        except FutureTimeoutError as exc:
            future.cancel()
            raise MarketDataDeadlineExceeded("data source task exceeded its batch deadline") from exc
        self._ensure_before_deadline(deadline)
        return result

    def _ensure_before_deadline(self, deadline: datetime | None) -> None:
        if deadline is not None and self._wall_clock() >= deadline:
            raise MarketDataDeadlineExceeded("market-data result completed after its batch deadline")


__all__ = ["MarketExecutionMixin"]
