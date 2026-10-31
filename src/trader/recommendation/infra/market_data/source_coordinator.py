"""Source-lane scheduling, cache integration and refresh operations."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, wait
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol, cast

from trader.infra.cache_contracts import (
    BoundedCache,
    BuildCacheIdentity,
    CacheIdentity,
    CacheIdentitySpec,
    CacheRequestKey,
    RequestKey,
)
from trader.infra.cancellation import CancellationEvent
from trader.infra.market_data.observations import SourceObservation
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.ports.market_data import MarketDataFailedError, MarketDataNoDataError
from trader.recommendation.application.runtime.schedule import PhaseAt, ShanghaiNow
from trader.recommendation.application.runtime.source_lanes import (
    SourceLaneScheduler,
    SourceRequestSupersededError,
)
from trader.recommendation.domain.market.models import (
    MarketQuote,
)
from trader.recommendation.infra.market_data.gateway_runtime import (
    _before_deadline,
    _cache_error_code,
    _elapsed,
    _SourceFetch,
    _strip_source,
)
from trader.recommendation.infra.market_data.provider_ports import (
    FullMarketFetcher,
    FullMarketSource,
)
from trader.recommendation.infra.normalization.merge import ObservationFromQuote


@dataclass(frozen=True)
class MarketSourceDependencies:
    tencent_market: FullMarketSource
    sina: FullMarketSource
    minimum_market_rows: int
    worker_pool: BoundedExecutor | None
    source_lanes: SourceLaneScheduler | None
    cache: BoundedCache[object] | None
    source_contracts: Mapping[str, str]
    config_version: str
    schema_version: str
    monotonic: Callable[[], float]
    wall_clock: Callable[[], datetime]
    full_market_primary_timeout_seconds: float
    full_market_observation_sink: Callable[[Sequence[SourceObservation]], None] | None = None
    full_market_fetchers: Mapping[str, FullMarketFetcher] | None = None


@dataclass(frozen=True)
class SourceObservationRequest:
    source: str
    dataset: str
    subject_key: str
    request: Mapping[str, object]
    fetcher: Callable[[], Sequence[MarketQuote]]
    observed_at: datetime
    force: bool
    deadline: datetime | None
    minimum_rows: int
    bypass_cache: bool = False
    cancellation: threading.Event | None = None


@dataclass(frozen=True)
class SourceLaneIdentityRequest:
    dataset: str
    source: str
    subject_key: str
    request: Mapping[str, object]
    observed_at: datetime
    force: bool
    deadline: datetime | None


@dataclass(frozen=True)
class _SourceRefreshRequest:
    source: str
    fetcher: Callable[[], Sequence[MarketQuote]]
    observed_at: datetime
    deadline: datetime | None
    minimum_rows: int


@dataclass(frozen=True)
class _MarketRouteContext:
    observed_at: datetime
    force: bool
    deadline: datetime | None
    cancellation: Mapping[str, threading.Event]
    fetchers: Mapping[str, Callable[[], Sequence[MarketQuote]]]


class MarketSourceTelemetry(Protocol):
    def record_planned(self, source: str) -> None: ...

    def fetch_physical(
        self,
        source: str,
        fetcher: Callable[[], Sequence[MarketQuote]],
        minimum_rows: int,
        cancellation: threading.Event | None = None,
    ) -> tuple[Sequence[MarketQuote], float]: ...

    def record_fetch_result(self, source: str, success: bool, started: float, error: str) -> None: ...

    def record_deadline(self, source: str) -> None: ...

    def record_superseded(self, source: str) -> None: ...

    def record_source_time(self, source: str, source_time: datetime) -> None: ...

    def record_local_latency(self, stage: str, duration_ms: float) -> None: ...


class MarketSourceCoordinator:
    def __init__(self, dependencies: MarketSourceDependencies, telemetry: MarketSourceTelemetry) -> None:
        self._tencent_market = dependencies.tencent_market
        self._sina = dependencies.sina
        self._minimum_market_rows = dependencies.minimum_market_rows
        self._worker_pool = dependencies.worker_pool
        self._source_lanes = dependencies.source_lanes
        self._cache = dependencies.cache
        self._source_contracts = dict(dependencies.source_contracts)
        self._config_version = dependencies.config_version
        self._schema_version = dependencies.schema_version
        self._monotonic = dependencies.monotonic
        self._wall_clock = dependencies.wall_clock
        self._primary_timeout_seconds = dependencies.full_market_primary_timeout_seconds
        self._full_market_observation_sink = dependencies.full_market_observation_sink
        self._full_market_fetchers = dict(dependencies.full_market_fetchers or {})
        self._telemetry = telemetry

    def fetch_market_sources(
        self,
        observed_at: datetime,
        *,
        force: bool,
        deadline: datetime | None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[_SourceFetch, ...]:
        cancellation = {"tencent_market": CancellationEvent(cancelled), "sina": CancellationEvent(cancelled)}
        fetchers = {
            "tencent_market": self._full_market_fetcher("tencent_market", deadline, cancellation["tencent_market"]),
            "sina": self._full_market_fetcher("sina", deadline, cancellation["sina"]),
        }
        context = _MarketRouteContext(observed_at, force, deadline, cancellation, fetchers)
        return self._fetch_primary_then_backup(context)

    def _fetch_primary_then_backup(
        self,
        context: _MarketRouteContext,
    ) -> tuple[_SourceFetch, ...]:
        primary_deadline = self._wall_clock() + timedelta(seconds=self._primary_timeout_seconds)
        if context.deadline is not None:
            primary_deadline = min(primary_deadline, context.deadline)
        primary = self._route_source("tencent_market", context, deadline=primary_deadline)
        if primary.status == "success":
            return (primary, _SourceFetch("sina", "skipped", error="backup_not_needed", skipped=True))
        if self._remaining_seconds(context.deadline) == 0.0 or context.cancellation["sina"].is_set():
            return (primary, _SourceFetch("sina", "skipped", error="deadline", skipped=True))
        return (primary, self._route_source("sina", context))

    def _route_source(
        self, source: str, context: _MarketRouteContext, *, deadline: datetime | None = None
    ) -> _SourceFetch:
        deadline = deadline or context.deadline
        if self._source_lanes is None:
            return self._market_source_result(source, context, deadline)
        future = self._submit_market_source(source, context, deadline)
        try:
            return future.result(timeout=self._remaining_seconds(deadline))
        except FutureTimeoutError:
            context.cancellation[source].set()
            if not future.cancel():
                wait((future,))
            self._telemetry.record_deadline(source)
            return _SourceFetch(source, "failed", error="deadline")
        except SourceRequestSupersededError:
            self._telemetry.record_superseded(source)
            return _SourceFetch(source, "skipped", error="superseded", skipped=True)
        except Exception as exc:
            return _SourceFetch(source, "failed", error=_cache_error_code(exc))

    def _submit_market_source(
        self,
        source: str,
        context: _MarketRouteContext,
        deadline: datetime | None,
    ) -> Future[_SourceFetch]:
        assert self._source_lanes is not None
        request = {"universe": "ashare", "fields": ["realtime_quote"]}
        identity = self.lane_identity(
            SourceLaneIdentityRequest(
                "full_market_quotes",
                source,
                "market",
                request,
                context.observed_at,
                context.force,
                deadline,
            )
        )
        return self._source_lanes.submit(
            source,
            identity,
            context.observed_at,
            self._market_source_result,
            source,
            context,
            deadline,
        )

    def _remaining_seconds(self, deadline: datetime | None) -> float | None:
        if deadline is None:
            return None
        return max(0.0, (deadline - self._wall_clock()).total_seconds())

    def _full_market_fetcher(
        self,
        source: str,
        deadline: datetime | None,
        cancellation: threading.Event,
    ) -> Callable[[], Sequence[MarketQuote]]:
        client = self._tencent_market if source == "tencent_market" else self._sina
        fetcher = self._full_market_fetchers.get(source)
        if fetcher is not None:
            return lambda: fetcher(deadline, cancellation)
        return client.fetch_market

    def _market_source_result(
        self,
        source: str,
        context: _MarketRouteContext,
        deadline: datetime | None,
    ) -> _SourceFetch:
        started = self._monotonic()
        try:
            observations = self.fetch_source_observations(
                SourceObservationRequest(
                    source,
                    "full_market_quotes",
                    "market",
                    {"universe": "ashare", "fields": ["realtime_quote"]},
                    context.fetchers[source],
                    context.observed_at,
                    context.force,
                    deadline,
                    self._minimum_market_rows,
                    cancellation=context.cancellation[source],
                )
            )
        except MarketDataNoDataError as exc:
            return _SourceFetch(
                source,
                "no_data",
                error=_strip_source(source, str(exc)),
                duration_ms=_elapsed(started, self._monotonic()),
            )
        except Exception as exc:
            error = _strip_source(source, str(exc))
            return _SourceFetch(
                source,
                "skipped" if error == "circuit_open" else "failed",
                error=error,
                skipped=error == "circuit_open",
                duration_ms=_elapsed(started, self._monotonic()),
            )
        return _SourceFetch(source, "success", observations, duration_ms=_elapsed(started, self._monotonic()))

    def fetch_source_observations(
        self,
        request: SourceObservationRequest,
    ) -> tuple[SourceObservation, ...]:
        source = request.source
        observed_at = request.observed_at
        if request.cancellation is not None and request.cancellation.is_set():
            raise MarketDataFailedError(source, "cancelled")
        self._telemetry.record_planned(source)
        if not _before_deadline(self._wall_clock(), request.deadline):
            if request.cancellation is None or not request.cancellation.is_set():
                self._telemetry.record_deadline(source)
            raise MarketDataFailedError(source, "late")
        identity = self._cache_identity(
            request.dataset,
            source,
            request.subject_key,
            request.request,
            observed_at,
        )

        def load() -> tuple[SourceObservation, ...]:
            return self._load_source_observations(request, identity)

        if self._cache is not None and not request.force and not request.bypass_cache:
            cached = self._cached_source_observations(identity, request)
            if cached is not None:
                return cached

        try:
            return (
                cast(tuple[SourceObservation, ...], self._cache.coalesce(identity, load))
                if self._cache is not None and not request.bypass_cache
                else load()
            )
        except Exception as exc:
            if (
                self._cache is not None
                and not request.bypass_cache
                and _before_deadline(self._wall_clock(), request.deadline)
                and (request.cancellation is None or not request.cancellation.is_set())
            ):
                self._cache.put_negative(identity, error_code=_cache_error_code(exc))
            raise

    def _load_source_observations(
        self, request: SourceObservationRequest, identity: CacheIdentity
    ) -> tuple[SourceObservation, ...]:
        source = request.source
        quotes, started = self._telemetry.fetch_physical(source, request.fetcher, request.minimum_rows, request.cancellation)
        if request.cancellation is not None and request.cancellation.is_set():
            raise MarketDataFailedError(source, "cancelled")
        completed_at = max(request.observed_at, self._wall_clock())
        normalization_started = self._monotonic()
        observations = tuple(ObservationFromQuote(quote, source=source, observed_at=completed_at) for quote in quotes)
        self._telemetry.record_local_latency(
            "normalization",
            _elapsed(normalization_started, self._monotonic()),
        )
        if request.deadline is not None and completed_at >= request.deadline:
            if request.cancellation is None or not request.cancellation.is_set():
                self._telemetry.record_fetch_result(source, False, started, "deadline")
            raise MarketDataFailedError(source, "late")
        if request.dataset == "full_market_quotes" and self._full_market_observation_sink is not None:
            self._full_market_observation_sink(observations)
        if self._cache is not None and not request.bypass_cache:
            source_time = max(observation.source_time for observation in observations)
            data_version = max(observation.data_version for observation in observations)
            self._cache.put(identity, observations, data_version=data_version, source_time=source_time)
        self._telemetry.record_fetch_result(source, True, started, "")
        self._telemetry.record_source_time(source, max(observation.source_time for observation in observations))
        return observations

    def _cached_source_observations(
        self,
        identity: CacheIdentity,
        request: SourceObservationRequest,
    ) -> tuple[SourceObservation, ...] | None:
        cache = self._cache
        assert cache is not None
        lookup = cache.get(identity)
        if lookup is None:
            return None
        if lookup.state == "negative":
            raise MarketDataFailedError(request.source, lookup.error_code or "negative_cache")
        if lookup.value is None:
            return None
        if request.dataset == "full_market_quotes" and lookup.state != "fresh":
            if lookup.error_code is not None and lookup.retry_suppressed:
                raise MarketDataFailedError(request.source, lookup.error_code)
            return None
        observations = cast(tuple[SourceObservation, ...], lookup.value)
        if lookup.state != "fresh":
            observations = _mark_observations_degraded(
                observations,
                "cache_refresh",
                f"cache_{lookup.state}",
            )
        if lookup.error_code is not None:
            observations = _mark_observations_degraded(
                observations,
                "cache_error",
                lookup.error_code,
            )
        if lookup.state != "fresh" and not lookup.retry_suppressed:
            self._schedule_refresh(
                identity,
                _SourceRefreshRequest(
                    request.source,
                    request.fetcher,
                    request.observed_at,
                    request.deadline,
                    request.minimum_rows,
                ),
            )
        return observations

    def _schedule_refresh(
        self,
        identity: CacheIdentity,
        request: _SourceRefreshRequest,
    ) -> None:
        if self._worker_pool is None or not self._worker_pool.is_running() or self._cache is None:
            return
        cache = self._cache
        worker_pool = self._worker_pool

        def refresh() -> None:
            def load() -> tuple[SourceObservation, ...]:
                quotes, started = self._telemetry.fetch_physical(request.source, request.fetcher, request.minimum_rows)
                completed_at = max(request.observed_at, self._wall_clock())
                if request.deadline is not None and completed_at >= request.deadline:
                    self._telemetry.record_fetch_result(request.source, False, started, "deadline")
                    raise MarketDataFailedError(request.source, "late")
                normalization_started = self._monotonic()
                observations = tuple(
                    ObservationFromQuote(quote, source=request.source, observed_at=completed_at) for quote in quotes
                )
                self._telemetry.record_local_latency(
                    "normalization",
                    _elapsed(normalization_started, self._monotonic()),
                )
                cache.put(
                    identity,
                    observations,
                    data_version=max(item.data_version for item in observations),
                    source_time=max(item.source_time for item in observations),
                )
                self._telemetry.record_fetch_result(request.source, True, started, "")
                self._telemetry.record_source_time(request.source, max(item.source_time for item in observations))
                return observations

            try:
                cache.coalesce(identity, load)
            except Exception as exc:
                if _before_deadline(self._wall_clock(), request.deadline):
                    cache.put_negative(identity, error_code=_cache_error_code(exc))
                return

        if self._source_lanes is not None:
            refresh_identity = RequestKey(
                {
                    "dataset": identity.dataset,
                    "source": identity.source,
                    "subject": identity.subject_key,
                    "request": identity.request_key,
                    "date": identity.trade_date,
                    "phase": identity.phase,
                    "contract": identity.source_contract_version,
                    "config": identity.config_version,
                    "schema": identity.schema_version,
                }
            )
            self._source_lanes.submit(request.source, refresh_identity, request.observed_at, refresh)
            return
        worker_pool.submit(refresh)

    def lane_identity(
        self,
        request: SourceLaneIdentityRequest,
    ) -> CacheRequestKey:
        cache_identity = self._cache_identity(
            request.dataset,
            request.source,
            request.subject_key,
            request.request,
            request.observed_at,
        )
        return RequestKey(
            {
                "dataset": cache_identity.dataset,
                "source": cache_identity.source,
                "subject": cache_identity.subject_key,
                "request": cache_identity.request_key,
                "date": cache_identity.trade_date,
                "phase": cache_identity.phase,
                "contract": cache_identity.source_contract_version,
                "config": cache_identity.config_version,
                "schema": cache_identity.schema_version,
                "force": request.force,
                "deadline": request.deadline,
            }
        )

    def _cache_identity(
        self,
        dataset: str,
        source: str,
        subject_key: str,
        request: Mapping[str, object],
        observed_at: datetime,
    ) -> CacheIdentity:
        local = ShanghaiNow(observed_at)
        phase = PhaseAt(local, is_trading_day=True).value
        return BuildCacheIdentity(
            CacheIdentitySpec(
                dataset=dataset,
                source=source,
                subject_key=subject_key,
                request=request,
                trade_date=local.date().isoformat(),
                phase=phase,
                source_contract_version=self._source_contracts[source],
                config_version=self._config_version,
                schema_version=self._schema_version,
            )
        )


def _mark_observations_degraded(
    observations: tuple[SourceObservation, ...],
    field: str,
    reason: str,
) -> tuple[SourceObservation, ...]:
    return tuple(
        replace(
            observation,
            missing_reasons={**dict(observation.missing_reasons), field: reason},
        )
        for observation in observations
    )


__all__ = ["MarketSourceCoordinator", "MarketSourceDependencies", "MarketSourceTelemetry"]
