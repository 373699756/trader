"""Shared bounded execution and deadline helpers for market-data services."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime
from typing import TYPE_CHECKING, ParamSpec, TypedDict, TypeVar

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.application.cache import BoundedCache, CacheIdentity, CacheIdentitySpec, build_cache_identity
from trader.application.ports.market import MarketDataDeadlineExceededError
from trader.application.schedule import phase_at, shanghai_now
from trader.application.source_lanes import SourceLaneRegistry
from trader.application.workers import BoundedExecutor

_P = ParamSpec("_P")
_T = TypeVar("_T")


class MarketTaskRunnerOptions(TypedDict):
    worker_pool: BoundedExecutor | None
    source_lanes: SourceLaneRegistry | None
    cache: BoundedCache[object] | None
    source_contract_versions: Mapping[str, str]
    config_version: str
    schema_version: str
    wall_clock: Callable[[], datetime]


class MarketTaskRunner:
    def __init__(
        self,
        **options: Unpack[MarketTaskRunnerOptions],
    ) -> None:
        self.worker_pool = options["worker_pool"]
        self.source_lanes = options["source_lanes"]
        self.cache = options["cache"]
        self.source_contract_versions = dict(options["source_contract_versions"])
        self.config_version = options["config_version"]
        self.schema_version = options["schema_version"]
        self.wall_clock = options["wall_clock"]

    def run_data_task(
        self,
        urgent: bool,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        pool = self.worker_pool
        if pool is None or not pool.is_running() or pool.owns_current_thread():
            return function(*args, **kwargs)
        submit = pool.submit_urgent if urgent else pool.submit
        future = submit(function, *args, **kwargs)
        if future is None:
            raise RuntimeError("data worker queue rejected source task")
        return future.result()

    def run_source_task(
        self,
        source: str,
        identity: str,
        observed_at: datetime,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        lanes = self.source_lanes
        if lanes is None or lanes.owns_current_thread(source):
            return function(*args, **kwargs)
        return lanes.submit(source, identity, observed_at, function, *args, **kwargs).result()

    def cache_identity(
        self,
        dataset: str,
        source: str,
        subject_key: str,
        request: Mapping[str, object],
        observed_at: datetime,
    ) -> CacheIdentity:
        local = shanghai_now(observed_at)
        return build_cache_identity(
            CacheIdentitySpec(
                dataset=dataset,
                source=source,
                subject_key=subject_key,
                request=request,
                trade_date=local.date().isoformat(),
                phase=phase_at(local, is_trading_day=True).value,
                source_contract_version=self.source_contract_versions.get(source, f"{source}-component-v1"),
                config_version=self.config_version,
                schema_version=self.schema_version,
            )
        )

    def run_data_task_until(
        self,
        deadline: datetime | None,
        urgent: bool,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        if deadline is None:
            if self.source_lanes is not None:
                return function(*args, **kwargs)
            return self.run_data_task(urgent, function, *args, **kwargs)
        self.ensure_before_deadline(deadline)
        if self.source_lanes is not None:
            result = function(*args, **kwargs)
            self.ensure_before_deadline(deadline)
            return result
        pool = self.worker_pool
        if pool is None or not pool.is_running() or pool.owns_current_thread():
            result = function(*args, **kwargs)
            self.ensure_before_deadline(deadline)
            return result
        submit = pool.submit_urgent if urgent else pool.submit
        future = submit(function, *args, **kwargs)
        if future is None:
            raise RuntimeError("data worker queue rejected deadline-bound source task")
        remaining = max(0.0, (deadline - self.wall_clock()).total_seconds())
        try:
            result = future.result(timeout=remaining)
        except FutureTimeoutError as exc:
            future.cancel()
            raise MarketDataDeadlineExceededError("data source task exceeded its batch deadline") from exc
        self.ensure_before_deadline(deadline)
        return result

    def ensure_before_deadline(self, deadline: datetime | None) -> None:
        if deadline is not None and self.wall_clock() >= deadline:
            raise MarketDataDeadlineExceededError("market-data result completed after its batch deadline")


__all__ = ["MarketTaskRunner"]
