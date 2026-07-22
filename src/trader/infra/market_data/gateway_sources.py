"""Source-lane scheduling, cache integration and refresh operations."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import replace
from datetime import datetime
from typing import Any, cast

from trader.application.cache import BoundedCache, CacheIdentity, build_cache_identity, canonical_json_bytes
from trader.application.ports import MarketDataFailed, MarketDataNoData
from trader.application.schedule import phase_at, shanghai_now
from trader.application.source_lanes import (
    SourceLaneRegistry,
    SourceRequestSuperseded,
)
from trader.application.workers import BoundedExecutor, borrow_executor
from trader.domain.models import CanonicalMarketSnapshot, MarketQuote
from trader.infra.market_data.eastmoney import EastmoneyClient
from trader.infra.market_data.gateway_support import (
    _before_deadline,
    _cache_error_code,
    _elapsed,
    _SourceFetch,
    _strip_source,
)
from trader.infra.market_data.merge import observation_from_quote
from trader.infra.market_data.observations import SourceObservation
from trader.infra.market_data.sina import SinaClient


class MarketGatewaySourcesMixin:
    _eastmoney: EastmoneyClient
    _sina: SinaClient
    _minimum_market_rows: int
    _worker_pool: BoundedExecutor | None
    _source_lanes: SourceLaneRegistry | None
    _cache: BoundedCache[object] | None
    _source_contract_versions: dict[str, str]
    _config_version: str
    _schema_version: str
    _monotonic: Callable[[], float]
    _wall_clock: Callable[[], datetime]
    _state_lock: Any
    _latest_snapshot: CanonicalMarketSnapshot | None

    def _fetch_market_sources(
        self,
        observed_at: datetime,
        *,
        force: bool,
        deadline: datetime | None,
    ) -> tuple[_SourceFetch, ...]:
        fetchers = {
            "eastmoney": self._eastmoney.fetch_market,
            "sina": self._sina.fetch_market,
        }
        if self._source_lanes is not None:
            futures: dict[str, Future[_SourceFetch]] = {}
            for source, fetcher in fetchers.items():
                request = {"universe": "ashare", "fields": ["realtime_quote"]}
                identity = self._lane_identity(
                    "full_market_quotes",
                    source,
                    "market",
                    request,
                    observed_at,
                    force=force,
                    deadline=deadline,
                )
                futures[source] = self._source_lanes.submit(
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
            results: dict[str, _SourceFetch] = {}
            for source, future in futures.items():
                try:
                    if deadline is None:
                        results[source] = future.result()
                    else:
                        remaining = max(0.0, (deadline - self._wall_clock()).total_seconds())
                        results[source] = future.result(timeout=remaining)
                except FutureTimeoutError:
                    future.cancel()
                    results[source] = _SourceFetch(source, "failed", error="deadline")
                except SourceRequestSuperseded:
                    results[source] = _SourceFetch(source, "skipped", error="superseded", skipped=True)
                except Exception as exc:
                    results[source] = _SourceFetch(source, "failed", error=_cache_error_code(exc))
            return tuple(results[source] for source in ("eastmoney", "sina"))

        fallback_futures: dict[str, Future[_SourceFetch]] = {}
        immediate: dict[str, _SourceFetch] = {}
        with borrow_executor(
            self._worker_pool,
            worker_count=2,
            queue_capacity=2,
            thread_name_prefix="source-data",
        ) as executor:
            for source, fetcher in fetchers.items():
                submitted = executor.submit(
                    self._market_source_result,
                    source,
                    fetcher,
                    observed_at,
                    force=force,
                    deadline=deadline,
                )
                if submitted is None:
                    immediate[source] = self._market_source_result(
                        source,
                        fetcher,
                        observed_at,
                        force=force,
                        deadline=deadline,
                    )
                else:
                    fallback_futures[source] = submitted
            results = {
                **immediate,
                **{source: future.result() for source, future in fallback_futures.items()},
            }
        return tuple(results[source] for source in ("eastmoney", "sina"))

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
            observations = self._fetch_source_observations(
                source,
                "full_market_quotes",
                "market",
                {"universe": "ashare", "fields": ["realtime_quote"]},
                fetcher,
                observed_at,
                force=force,
                deadline=deadline,
                minimum_rows=self._minimum_market_rows,
            )
        except MarketDataNoData as exc:
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

    def _fetch_source_observations(
        self,
        source: str,
        dataset: str,
        subject_key: str,
        request: Mapping[str, object],
        fetcher: Callable[[], Sequence[MarketQuote]],
        observed_at: datetime,
        *,
        force: bool,
        deadline: datetime | None,
        minimum_rows: int,
    ) -> tuple[SourceObservation, ...]:
        self._record_planned(source)
        if not _before_deadline(self._wall_clock(), deadline):
            self._record_deadline(source)
            raise MarketDataFailed(source, "late")
        identity = self._cache_identity(dataset, source, subject_key, request, observed_at)

        def load() -> tuple[SourceObservation, ...]:
            quotes, started = self._fetch_physical(source, fetcher, minimum_rows)
            completed_at = max(observed_at, self._wall_clock())
            if deadline is not None and completed_at >= deadline:
                self._record_fetch_result(source, False, started, "deadline")
                raise MarketDataFailed(source, "late")
            observations = tuple(
                observation_from_quote(quote, source=source, observed_at=completed_at) for quote in quotes
            )
            if self._cache is not None:
                source_time = max(observation.source_time for observation in observations)
                data_version = max(observation.data_version for observation in observations)
                self._cache.put(identity, observations, data_version=data_version, source_time=source_time)
            self._record_fetch_result(source, True, started, "")
            self._record_source_time(source, max(observation.source_time for observation in observations))
            return observations

        if self._cache is not None and not force:
            lookup = self._cache.get(identity)
            if lookup is not None and lookup.state == "negative":
                raise MarketDataFailed(source, lookup.error_code or "negative_cache")
            if lookup is not None and lookup.value is not None:
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
                    self._schedule_refresh(identity, source, fetcher, observed_at, deadline, minimum_rows)
                return observations

        try:
            return (
                cast(tuple[SourceObservation, ...], self._cache.coalesce(identity, load))
                if self._cache is not None
                else load()
            )
        except Exception as exc:
            if self._cache is not None and _before_deadline(self._wall_clock(), deadline):
                self._cache.put_negative(identity, error_code=_cache_error_code(exc))
            raise

    def _schedule_refresh(
        self,
        identity: CacheIdentity,
        source: str,
        fetcher: Callable[[], Sequence[MarketQuote]],
        observed_at: datetime,
        deadline: datetime | None,
        minimum_rows: int,
    ) -> None:
        if self._worker_pool is None or not self._worker_pool.is_running() or self._cache is None:
            return
        cache = self._cache
        worker_pool = self._worker_pool

        def refresh() -> None:
            def load() -> tuple[SourceObservation, ...]:
                quotes, started = self._fetch_physical(source, fetcher, minimum_rows)
                completed_at = max(observed_at, self._wall_clock())
                if deadline is not None and completed_at >= deadline:
                    self._record_fetch_result(source, False, started, "deadline")
                    raise MarketDataFailed(source, "late")
                observations = tuple(
                    observation_from_quote(quote, source=source, observed_at=completed_at) for quote in quotes
                )
                cache.put(
                    identity,
                    observations,
                    data_version=max(item.data_version for item in observations),
                    source_time=max(item.source_time for item in observations),
                )
                self._record_fetch_result(source, True, started, "")
                self._record_source_time(source, max(item.source_time for item in observations))
                return observations

            try:
                cache.coalesce(identity, load)
            except Exception as exc:
                if _before_deadline(self._wall_clock(), deadline):
                    cache.put_negative(identity, error_code=_cache_error_code(exc))
                return

        if self._source_lanes is not None:
            refresh_identity = "refresh:" + hashlib.sha256(canonical_json_bytes(identity.as_dict())).hexdigest()
            self._source_lanes.submit(source, refresh_identity, observed_at, refresh)
            return
        worker_pool.submit(refresh)

    def _lane_identity(
        self,
        dataset: str,
        source: str,
        subject_key: str,
        request: Mapping[str, object],
        observed_at: datetime,
        *,
        force: bool,
        deadline: datetime | None,
    ) -> str:
        cache_identity = self._cache_identity(dataset, source, subject_key, request, observed_at)
        digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "cache_identity": cache_identity.as_dict(),
                    "force": force,
                    "deadline": deadline,
                }
            )
        ).hexdigest()
        return f"{dataset}:{digest}"

    def _mark_snapshot_degraded(self, reason: str, _observed_at: datetime) -> None:
        with self._state_lock:
            if self._latest_snapshot is None:
                return
            self._latest_snapshot = replace(
                self._latest_snapshot,
                degraded_reasons=tuple(sorted({*self._latest_snapshot.degraded_reasons, reason})),
            )

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

    def _record_planned(self, source: str) -> None:
        raise NotImplementedError

    def _fetch_physical(
        self,
        source: str,
        fetcher: Callable[[], Sequence[MarketQuote]],
        minimum_rows: int,
    ) -> tuple[Sequence[MarketQuote], float]:
        raise NotImplementedError

    def _record_fetch_result(self, source: str, success: bool, started: float, error: str) -> None:
        raise NotImplementedError

    def _record_deadline(self, source: str) -> None:
        raise NotImplementedError

    def _record_source_time(self, source: str, source_time: datetime) -> None:
        raise NotImplementedError


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


__all__ = ["MarketGatewaySourcesMixin"]
