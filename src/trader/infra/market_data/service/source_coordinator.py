"""Source-lane scheduling, cache integration and refresh operations."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, cast

from trader.application.cache import (
    BoundedCache,
    CacheIdentity,
    CacheIdentitySpec,
    build_cache_identity,
    canonical_json_bytes,
)
from trader.application.ports.market import MarketDataFailedError, MarketDataNoDataError
from trader.application.schedule import phase_at, shanghai_now
from trader.application.source_lanes import (
    SourceLaneRegistry,
    SourceRequestSupersededError,
)
from trader.application.workers import BoundedExecutor
from trader.domain.market.models import (
    MarketQuote,
)
from trader.infra.market_data.normalization.merge import observation_from_quote
from trader.infra.market_data.providers.eastmoney import EastmoneyClient
from trader.infra.market_data.providers.sina import SinaClient
from trader.infra.market_data.service.gateway_runtime import (
    _before_deadline,
    _cache_error_code,
    _elapsed,
    _SourceFetch,
    _strip_source,
)
from trader.infra.market_data.service.observations import SourceObservation


@dataclass(frozen=True)
class MarketSourceDependencies:
    eastmoney: EastmoneyClient
    sina: SinaClient
    minimum_market_rows: int
    worker_pool: BoundedExecutor | None
    source_lanes: SourceLaneRegistry | None
    cache: BoundedCache[object] | None
    source_contract_versions: Mapping[str, str]
    config_version: str
    schema_version: str
    monotonic: Callable[[], float]
    wall_clock: Callable[[], datetime]
    full_market_hedge_delay_seconds: float
    full_market_observation_sink: Callable[[Sequence[SourceObservation]], None] | None = None


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
    ) -> tuple[Sequence[MarketQuote], float]: ...

    def record_fetch_result(self, source: str, success: bool, started: float, error: str) -> None: ...

    def record_deadline(self, source: str) -> None: ...

    def record_superseded(self, source: str) -> None: ...

    def record_source_time(self, source: str, source_time: datetime) -> None: ...

    def record_local_latency(self, stage: str, duration_ms: float) -> None: ...


class MarketSourceCoordinator:
    def __init__(self, dependencies: MarketSourceDependencies, telemetry: MarketSourceTelemetry) -> None:
        self._eastmoney = dependencies.eastmoney
        self._sina = dependencies.sina
        self._minimum_market_rows = dependencies.minimum_market_rows
        self._worker_pool = dependencies.worker_pool
        self._source_lanes = dependencies.source_lanes
        self._cache = dependencies.cache
        self._source_contract_versions = dict(dependencies.source_contract_versions)
        self._config_version = dependencies.config_version
        self._schema_version = dependencies.schema_version
        self._monotonic = dependencies.monotonic
        self._wall_clock = dependencies.wall_clock
        self._full_market_hedge_delay_seconds = dependencies.full_market_hedge_delay_seconds
        self._full_market_observation_sink = dependencies.full_market_observation_sink
        self._telemetry = telemetry

    def fetch_market_sources(
        self,
        observed_at: datetime,
        *,
        force: bool,
        deadline: datetime | None,
    ) -> tuple[_SourceFetch, ...]:
        cancellation = {"eastmoney": threading.Event(), "sina": threading.Event()}
        fetchers = {
            "eastmoney": self._full_market_fetcher("eastmoney", deadline, cancellation["eastmoney"]),
            "sina": self._full_market_fetcher("sina", deadline, cancellation["sina"]),
        }
        context = _MarketRouteContext(observed_at, force, deadline, cancellation, fetchers)
        if self._source_lanes is None:
            return self._fetch_market_without_lanes(context)
        return self._fetch_hedged_market_sources(context)

    def _fetch_market_without_lanes(
        self,
        context: _MarketRouteContext,
    ) -> tuple[_SourceFetch, ...]:
        primary = self._market_source_result(
            "eastmoney",
            context.fetchers["eastmoney"],
            context.observed_at,
            force=context.force,
            deadline=context.deadline,
        )
        if primary.status == "success":
            return (primary, _SourceFetch("sina", "skipped", error="hedge_not_needed", skipped=True))
        fallback = self._market_source_result(
            "sina",
            context.fetchers["sina"],
            context.observed_at,
            force=context.force,
            deadline=context.deadline,
        )
        return (primary, fallback)

    def _fetch_hedged_market_sources(
        self,
        context: _MarketRouteContext,
    ) -> tuple[_SourceFetch, ...]:
        primary_future = self._submit_market_source(
            "eastmoney",
            context.fetchers["eastmoney"],
            context.observed_at,
            force=context.force,
            deadline=context.deadline,
        )
        primary = self._await_primary_hedge(primary_future, context.deadline)
        if primary is not None and primary.status == "success":
            return (primary, _SourceFetch("sina", "skipped", error="hedge_not_needed", skipped=True))
        fallback_future = self._submit_market_source(
            "sina",
            context.fetchers["sina"],
            context.observed_at,
            force=context.force,
            deadline=context.deadline,
        )
        return self._await_hedged_result(context, primary_future, fallback_future, primary)

    def _await_primary_hedge(
        self,
        primary_future: Future[_SourceFetch],
        deadline: datetime | None,
    ) -> _SourceFetch | None:
        hedge_wait = self._remaining_seconds(deadline, cap=self._full_market_hedge_delay_seconds)
        try:
            return primary_future.result(timeout=hedge_wait)
        except FutureTimeoutError:
            return None
        except SourceRequestSupersededError:
            self._telemetry.record_superseded("eastmoney")
            return _SourceFetch("eastmoney", "skipped", error="superseded", skipped=True)
        except Exception as exc:
            return _SourceFetch("eastmoney", "failed", error=_cache_error_code(exc))

    def _await_hedged_result(
        self,
        context: _MarketRouteContext,
        primary_future: Future[_SourceFetch],
        fallback_future: Future[_SourceFetch],
        primary: _SourceFetch | None,
    ) -> tuple[_SourceFetch, ...]:
        results = {} if primary is None else {"eastmoney": primary}
        pending: dict[str, Future[_SourceFetch]] = {"sina": fallback_future}
        if primary is None:
            pending["eastmoney"] = primary_future
        while pending:
            remaining = self._remaining_seconds(context.deadline)
            if remaining == 0.0:
                break
            done, _not_done = wait(
                tuple(pending.values()),
                timeout=remaining,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                break
            completed = self._consume_completed_hedge(context, done, pending, results)
            if completed is not None:
                return completed
        for source, future in pending.items():
            context.cancellation[source].set()
            if future.cancel():
                self._telemetry.record_deadline(source)
            results[source] = _SourceFetch(source, "failed", error="deadline")
        return tuple(results.get(source, _missing_hedge_result(source)) for source in ("eastmoney", "sina"))

    def _consume_completed_hedge(
        self,
        context: _MarketRouteContext,
        done: set[Future[_SourceFetch]],
        pending: dict[str, Future[_SourceFetch]],
        results: dict[str, _SourceFetch],
    ) -> tuple[_SourceFetch, ...] | None:
        for source in ("eastmoney", "sina"):
            future = pending.get(source)
            if future is None or future not in done:
                continue
            result = self._resolve_market_future(source, future)
            results[source] = result
            pending.pop(source)
            if result.status != "success":
                continue
            for loser, loser_future in tuple(pending.items()):
                cancelled = loser_future.cancel()
                if cancelled:
                    context.cancellation[loser].set()
                results[loser] = _SourceFetch(
                    loser,
                    "skipped",
                    error="hedge_cancelled" if cancelled else "hedge_inflight",
                    skipped=True,
                )
                pending.pop(loser)
            return tuple(results.get(name, _missing_hedge_result(name)) for name in ("eastmoney", "sina"))
        return None

    def _submit_market_source(
        self,
        source: str,
        fetcher: Callable[[], Sequence[MarketQuote]],
        observed_at: datetime,
        *,
        force: bool,
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
                observed_at,
                force,
                deadline,
            )
        )
        return self._source_lanes.submit(
            source,
            identity,
            observed_at,
            self._market_source_result,
            source,
            fetcher,
            observed_at,
            force=force,
            deadline=deadline,
        )

    def _resolve_market_future(
        self,
        source: str,
        future: Future[_SourceFetch],
    ) -> _SourceFetch:
        try:
            return future.result()
        except SourceRequestSupersededError:
            self._telemetry.record_superseded(source)
            return _SourceFetch(source, "skipped", error="superseded", skipped=True)
        except Exception as exc:
            return _SourceFetch(source, "failed", error=_cache_error_code(exc))

    def _remaining_seconds(self, deadline: datetime | None, *, cap: float | None = None) -> float | None:
        if deadline is None:
            return cap
        remaining = max(0.0, (deadline - self._wall_clock()).total_seconds())
        return remaining if cap is None else min(remaining, cap)

    def _full_market_fetcher(
        self,
        source: str,
        deadline: datetime | None,
        cancellation: threading.Event,
    ) -> Callable[[], Sequence[MarketQuote]]:
        client = self._eastmoney if source == "eastmoney" else self._sina
        if isinstance(client, (EastmoneyClient, SinaClient)):
            return lambda: client.fetch_market(deadline=deadline, cancel_event=cancellation)
        return client.fetch_market

    def _market_source_result(
        self,
        source: str,
        fetcher: Callable[[], Sequence[MarketQuote]],
        observed_at: datetime,
        *,
        force: bool,
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
                    fetcher,
                    observed_at,
                    force,
                    deadline,
                    self._minimum_market_rows,
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
        self._telemetry.record_planned(source)
        if not _before_deadline(self._wall_clock(), request.deadline):
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
            quotes, started = self._telemetry.fetch_physical(source, request.fetcher, request.minimum_rows)
            completed_at = max(observed_at, self._wall_clock())
            normalization_started = self._monotonic()
            observations = tuple(
                observation_from_quote(quote, source=source, observed_at=completed_at) for quote in quotes
            )
            self._telemetry.record_local_latency(
                "normalization",
                _elapsed(normalization_started, self._monotonic()),
            )
            if request.dataset == "full_market_quotes" and self._full_market_observation_sink is not None:
                self._full_market_observation_sink(observations)
            if request.deadline is not None and completed_at >= request.deadline:
                self._telemetry.record_fetch_result(source, False, started, "deadline")
                raise MarketDataFailedError(source, "late")
            if self._cache is not None and not request.bypass_cache:
                source_time = max(observation.source_time for observation in observations)
                data_version = max(observation.data_version for observation in observations)
                self._cache.put(identity, observations, data_version=data_version, source_time=source_time)
            self._telemetry.record_fetch_result(source, True, started, "")
            self._telemetry.record_source_time(source, max(observation.source_time for observation in observations))
            return observations

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
            ):
                self._cache.put_negative(identity, error_code=_cache_error_code(exc))
            raise

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
                    observation_from_quote(quote, source=request.source, observed_at=completed_at) for quote in quotes
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
            refresh_identity = "refresh:" + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
            self._source_lanes.submit(request.source, refresh_identity, request.observed_at, refresh)
            return
        worker_pool.submit(refresh)

    def lane_identity(
        self,
        request: SourceLaneIdentityRequest,
    ) -> str:
        cache_identity = self._cache_identity(
            request.dataset,
            request.source,
            request.subject_key,
            request.request,
            request.observed_at,
        )
        digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "cache_identity": cache_identity,
                    "force": request.force,
                    "deadline": request.deadline,
                }
            )
        ).hexdigest()
        return f"{request.dataset}:{digest}"

    def _cache_identity(
        self,
        dataset: str,
        source: str,
        subject_key: str,
        request: Mapping[str, object],
        observed_at: datetime,
    ) -> CacheIdentity:
        local = shanghai_now(observed_at)
        phase = phase_at(local, is_trading_day=True).value
        return build_cache_identity(
            CacheIdentitySpec(
                dataset=dataset,
                source=source,
                subject_key=subject_key,
                request=request,
                trade_date=local.date().isoformat(),
                phase=phase,
                source_contract_version=self._source_contract_versions[source],
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


def _missing_hedge_result(source: str) -> _SourceFetch:
    return _SourceFetch(source, "skipped", error="hedge_not_needed", skipped=True)


__all__ = ["MarketSourceCoordinator", "MarketSourceDependencies", "MarketSourceTelemetry"]
