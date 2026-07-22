"""Parallel market-source collection, deterministic merge and source health."""

from __future__ import annotations

import hashlib
import threading
import time
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import replace
from datetime import date, datetime, timezone

from trader.application.cache import BoundedCache, canonical_json_bytes
from trader.application.ports.market import (
    MarketDataDeadlineExceededError,
    MarketDataFailedError,
    MarketDataNoDataError,
    MarketDataUnavailableError,
)
from trader.application.schedule import shanghai_now
from trader.application.source_lanes import SourceLaneRegistry, SourceRequestSuperseded
from trader.application.workers import BoundedExecutor
from trader.domain.market.models import (
    CanonicalMarketSnapshot,
    MarketQuote,
)
from trader.infra.market_data.eastmoney import EastmoneyClient
from trader.infra.market_data.gateway_sources import MarketGatewaySourcesMixin
from trader.infra.market_data.gateway_support import (
    _cache_error_code,
    _canonical_health,
    _CircuitState,
    _observation_version,
    _parallel_error_message,
    _parallel_route_outcome,
    _percentile,
    _preserve_newer_quotes,
    _reference_replaces,
    _route_health,
    _SingleFlight,
    _source_degraded_reasons,
)
from trader.infra.market_data.merge import (
    merge_market_observations,
    observation_from_quote,
    overlay_canonical_snapshot,
)
from trader.infra.market_data.merge_quote import rejection_reason, source_name
from trader.infra.market_data.observations import SourceObservation
from trader.infra.market_data.router import RouteOutcome
from trader.infra.market_data.sina import SinaClient
from trader.infra.market_data.tencent import TencentClient


class MarketDataGateway(MarketGatewaySourcesMixin):
    def __init__(
        self,
        eastmoney: EastmoneyClient,
        sina: SinaClient,
        tencent: TencentClient,
        *,
        minimum_market_rows: int,
        circuit_breaker_failures: int,
        circuit_breaker_seconds: int,
        worker_pool: BoundedExecutor | None = None,
        source_lanes: SourceLaneRegistry | None = None,
        cache: BoundedCache[object] | None = None,
        source_contract_versions: Mapping[str, str] | None = None,
        config_version: str = "component-default",
        schema_version: str = "market-v15",
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._eastmoney = eastmoney
        self._sina = sina
        self._tencent = tencent
        self._minimum_market_rows = minimum_market_rows
        self._failure_limit = circuit_breaker_failures
        self._breaker_seconds = circuit_breaker_seconds
        self._worker_pool = worker_pool
        self._source_lanes = source_lanes
        self._cache = cache
        self._source_contract_versions = dict(
            source_contract_versions
            or {
                "eastmoney": "eastmoney-component-v1",
                "sina": "sina-component-v1",
                "tencent": "tencent-component-v1",
            }
        )
        self._config_version = config_version
        self._schema_version = schema_version
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._market_flight: _SingleFlight[Sequence[MarketQuote]] = _SingleFlight()
        self._candidate_fetch_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._states = {"eastmoney": _CircuitState(), "sina": _CircuitState(), "tencent": _CircuitState()}
        self._latest_by_code: dict[str, MarketQuote] = {}
        self._latest_observations: dict[str, dict[str, SourceObservation]] = {}
        self._reference_observations: dict[str, SourceObservation] = {}
        self._calendar_open_dates: set[date] = set()
        self._calendar_open_dates_sorted: tuple[date, ...] = ()
        self._latest_snapshot: CanonicalMarketSnapshot | None = None
        self._latest_source = "unavailable"
        self._last_route_outcome: RouteOutcome | None = None
        self._merge_count = 0
        self._conflict_count = 0

    def fetch_market(
        self,
        *,
        observed_at: datetime | None = None,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[MarketQuote]:
        requested_at = observed_at or self._wall_clock()
        if self._source_lanes is not None:
            return self._fetch_market_once(requested_at, force=force, deadline=deadline)
        return self._market_flight.run(lambda: self._fetch_market_once(requested_at, force=force, deadline=deadline))

    def _fetch_market_once(
        self,
        observed_at: datetime,
        *,
        force: bool,
        deadline: datetime | None,
    ) -> Sequence[MarketQuote]:
        results = self._fetch_market_sources(observed_at, force=force, deadline=deadline)
        successes = tuple(result for result in results if result.status == "success")
        outcome = _parallel_route_outcome(results)
        completed_at = max(observed_at, self._wall_clock())
        if deadline is not None and completed_at >= deadline:
            with self._state_lock:
                self._last_route_outcome = outcome
            raise MarketDataDeadlineExceededError("market data deadline exceeded before canonical merge")
        if not successes:
            with self._state_lock:
                self._last_route_outcome = outcome
                cached = tuple(self._latest_by_code.values())
                if self._latest_snapshot is not None:
                    self._latest_snapshot = replace(
                        self._latest_snapshot,
                        degraded_reasons=tuple(
                            sorted(
                                {
                                    *self._latest_snapshot.degraded_reasons,
                                    *_source_degraded_reasons(results),
                                    "all_sources_failed:last_valid_snapshot",
                                }
                            )
                        ),
                    )
            if cached:
                return cached
            raise MarketDataUnavailableError("market data unavailable: " + _parallel_error_message(results))
        observations = tuple(observation for result in successes for observation in result.observations)
        with self._state_lock:
            previous = self._latest_snapshot
            references = tuple(self._reference_observations.values())
            self._remember_observations_locked(observations, completed_at)
        snapshot = merge_market_observations(
            (*observations, *references),
            observed_at=completed_at,
            previous=previous,
        )
        snapshot = replace(
            snapshot,
            degraded_reasons=tuple(sorted({*snapshot.degraded_reasons, *_source_degraded_reasons(results)})),
        )
        while True:
            with self._state_lock:
                latest = self._latest_snapshot
            commit_snapshot = _preserve_newer_quotes(snapshot, latest)
            with self._state_lock:
                if self._latest_snapshot is not latest:
                    continue
                self._latest_snapshot = commit_snapshot
                self._latest_by_code = {quote.code: quote for quote in commit_snapshot.quotes}
                self._latest_source = "eastmoney+sina" if len(successes) == 2 else outcome.vendor
                self._last_route_outcome = outcome
                self._merge_count += 1
                self._conflict_count += len(commit_snapshot.conflicts)
                return tuple(self._latest_by_code.values())

    def fetch_candidates(
        self,
        codes: Sequence[str],
        *,
        observed_at: datetime | None = None,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[MarketQuote]:
        if not codes:
            return ()
        requested_at = observed_at or self._wall_clock()
        if self._source_lanes is not None:
            return self._fetch_candidates_once(codes, requested_at, force=force, deadline=deadline)
        with self._candidate_fetch_lock:
            return self._fetch_candidates_once(codes, requested_at, force=force, deadline=deadline)

    def _fetch_candidates_once(
        self,
        codes: Sequence[str],
        requested_at: datetime,
        *,
        force: bool,
        deadline: datetime | None,
    ) -> Sequence[MarketQuote]:
        normalized_codes = tuple(sorted(set(codes)))
        try:
            if self._source_lanes is None:
                observations = self._fetch_source_observations(
                    "tencent",
                    "candidate_quotes",
                    ",".join(normalized_codes),
                    {"codes": normalized_codes, "fields": ["realtime_quote"]},
                    lambda: self._tencent.fetch_quotes(normalized_codes),
                    requested_at,
                    force=force,
                    deadline=deadline,
                    minimum_rows=1,
                )
            else:
                request = {"codes": normalized_codes, "fields": ["realtime_quote"]}
                identity = self._lane_identity(
                    "candidate_quotes",
                    "tencent",
                    ",".join(normalized_codes),
                    request,
                    requested_at,
                    force=force,
                    deadline=deadline,
                )
                lane_future = self._source_lanes.submit_urgent(
                    "tencent",
                    identity,
                    requested_at,
                    self._fetch_source_observations,
                    "tencent",
                    "candidate_quotes",
                    ",".join(normalized_codes),
                    request,
                    lambda: self._tencent.fetch_quotes(normalized_codes),
                    requested_at,
                    force=force,
                    deadline=deadline,
                    minimum_rows=1,
                )
                if deadline is None:
                    observations = lane_future.result()
                else:
                    remaining = max(0.0, (deadline - self._wall_clock()).total_seconds())
                    try:
                        observations = lane_future.result(timeout=remaining)
                    except FutureTimeoutError:
                        lane_future.cancel()
                        raise
        except FutureTimeoutError:
            self._mark_snapshot_degraded("tencent:late", max(requested_at, self._wall_clock()))
            with self._state_lock:
                return tuple(self._latest_by_code[code] for code in codes if code in self._latest_by_code)
        except SourceRequestSuperseded:
            self._mark_snapshot_degraded("tencent:superseded", requested_at)
            with self._state_lock:
                return tuple(self._latest_by_code[code] for code in codes if code in self._latest_by_code)
        except Exception as exc:
            self._mark_snapshot_degraded(f"tencent:{_cache_error_code(exc)}", requested_at)
            with self._state_lock:
                return tuple(self._latest_by_code[code] for code in codes if code in self._latest_by_code)
        completed_at = max(requested_at, self._wall_clock())
        if deadline is not None and completed_at >= deadline:
            self._mark_snapshot_degraded("tencent:late", completed_at)
            with self._state_lock:
                return tuple(self._latest_by_code[code] for code in codes if code in self._latest_by_code)
        with self._state_lock:
            baseline = tuple(self._latest_by_code[code] for code in codes if code in self._latest_by_code)
            refreshed_sources = {
                (observation.subject_key, source_name(observation.source)) for observation in observations
            }
            raw_baseline = tuple(
                observation
                for code in normalized_codes
                for observation in self._latest_observations.get(code, {}).values()
                if (observation.subject_key, source_name(observation.source)) not in refreshed_sources
            )
            references = tuple(
                observation
                for code, observation in self._reference_observations.items()
                if code in set(normalized_codes)
            )
        raw_codes = {observation.subject_key for observation in raw_baseline}
        baseline_observations = tuple(
            observation_from_quote(quote, source=quote.source, observed_at=completed_at)
            for quote in baseline
            if quote.code not in raw_codes
        )
        snapshot = merge_market_observations(
            (*raw_baseline, *baseline_observations, *observations, *references),
            observed_at=completed_at,
            targeted_codes=codes,
        )
        with self._state_lock:
            self._remember_observations_locked(observations, completed_at)
            self._latest_snapshot = overlay_canonical_snapshot(self._latest_snapshot, snapshot)
            self._latest_by_code = {quote.code: quote for quote in self._latest_snapshot.quotes}
            self._merge_count += 1
            self._conflict_count += len(snapshot.conflicts)
            return tuple(self._latest_by_code[code] for code in codes if code in self._latest_by_code)

    def update_reference_observations(self, observations: Sequence[SourceObservation]) -> None:
        with self._state_lock:
            calendar_changed = False
            for observation in observations:
                if observation.status != "success" or observation.fields.get("is_open") is not True:
                    continue
                try:
                    open_date = date.fromisoformat(observation.subject_key)
                except ValueError:
                    continue
                if open_date not in self._calendar_open_dates:
                    self._calendar_open_dates.add(open_date)
                    calendar_changed = True
            if calendar_changed:
                self._calendar_open_dates_sorted = tuple(sorted(self._calendar_open_dates))
            for observation in observations:
                if observation.status != "success" or len(observation.subject_key) != 6:
                    continue
                observation = self._with_listing_sessions(observation)
                current = self._reference_observations.get(observation.subject_key)
                if current is None or _reference_replaces(current, observation):
                    self._reference_observations[observation.subject_key] = observation

    def _with_listing_sessions(self, observation: SourceObservation) -> SourceObservation:
        listing_raw = observation.fields.get("listing_date")
        open_dates = self._calendar_open_dates_sorted
        if not isinstance(listing_raw, str) or not open_dates:
            return observation
        try:
            listing_date = date.fromisoformat(listing_raw)
        except ValueError:
            return observation
        observed_date = shanghai_now(observation.observed_at).date()
        sessions = bisect_right(open_dates, observed_date) - bisect_left(open_dates, listing_date)
        if sessions <= 0:
            return observation
        fields = dict(observation.fields)
        fields["listing_age_sessions"] = float(sessions)
        fields["has_price_limit"] = sessions >= 6
        board = str(fields.get("board") or "")
        fields["exchange_limit_pct"] = (20.0 if board in {"chinext", "star"} else 10.0) if sessions >= 6 else None
        return replace(
            observation,
            fields=fields,
            payload_hash=hashlib.sha256(canonical_json_bytes(fields)).hexdigest(),
        )

    def _remember_observations_locked(
        self,
        observations: Sequence[SourceObservation],
        observed_at: datetime,
    ) -> None:
        for observation in observations:
            if rejection_reason(observation, observed_at) is not None:
                continue
            by_source = self._latest_observations.setdefault(observation.subject_key, {})
            source = observation.source.strip().lower()
            current = by_source.get(source)
            if current is None or _observation_version(observation) >= _observation_version(current):
                by_source[source] = observation

    def canonical_snapshot(self) -> CanonicalMarketSnapshot | None:
        with self._state_lock:
            return self._latest_snapshot

    def current_quotes(self, codes: Sequence[str]) -> Sequence[MarketQuote]:
        with self._state_lock:
            return tuple(self._latest_by_code[code] for code in codes if code in self._latest_by_code)

    def health(self) -> Mapping[str, object]:
        now = self._monotonic()
        measured_at = self._wall_clock()
        with self._state_lock:
            return {
                "active_source": self._latest_source,
                "cached_rows": len(self._latest_by_code),
                "merge_count": self._merge_count,
                "conflict_count": self._conflict_count,
                "merge_epoch": self._latest_snapshot.merge_epoch if self._latest_snapshot is not None else None,
                "canonical_snapshot": _canonical_health(self._latest_snapshot),
                "route": _route_health(self._last_route_outcome),
                "source_lanes": self._source_lanes.status() if self._source_lanes is not None else {},
                "sources": {
                    name: {
                        "planned_count": state.planned_count,
                        "success_count": state.success_count,
                        "error_count": state.error_count,
                        "timeout_count": state.timeout_count,
                        "consecutive_failures": state.failures,
                        "circuit_open": state.open_until > now,
                        "last_latency_ms": round(state.last_latency_ms, 2),
                        "p50_latency_ms": _percentile(state.latencies_ms, 0.50),
                        "p95_latency_ms": _percentile(state.latencies_ms, 0.95),
                        "last_error": state.last_error,
                        "data_age_seconds": max(0.0, (measured_at - state.last_source_time).total_seconds())
                        if state.last_source_time is not None
                        else None,
                    }
                    for name, state in self._states.items()
                },
                "cache": self._cache.status() if self._cache is not None else {},
            }

    def _record_planned(self, source: str) -> None:
        with self._state_lock:
            self._states[source].planned_count += 1

    def _is_open(self, source: str) -> bool:
        with self._state_lock:
            return self._states[source].open_until > self._monotonic()

    def _fetch_physical(
        self,
        source: str,
        fetcher: Callable[[], Sequence[MarketQuote]],
        minimum_rows: int,
    ) -> tuple[Sequence[MarketQuote], float]:
        if self._is_open(source):
            self._record_skipped_open(source)
            raise MarketDataFailedError(source, "circuit_open")
        started = self._monotonic()
        try:
            quotes = tuple(fetcher())
        except MarketDataNoDataError as exc:
            self._record(source, False, started, str(exc))
            raise
        except Exception as exc:
            self._record(source, False, started, str(exc))
            raise MarketDataFailedError(source, str(exc)) from exc
        if len(quotes) < minimum_rows:
            error = MarketDataNoDataError(f"{source}: only {len(quotes)} market rows")
            self._record(source, False, started, str(error))
            raise error
        return quotes, started

    def _record_fetch_result(self, source: str, success: bool, started: float, error: str) -> None:
        self._record(source, success, started, error)

    def _record_deadline(self, source: str) -> None:
        self._record(source, False, self._monotonic(), "deadline")

    def _record(self, source: str, success: bool, started: float, error: str) -> None:
        elapsed_ms = (self._monotonic() - started) * 1000.0
        with self._state_lock:
            state = self._states[source]
            state.last_latency_ms = elapsed_ms
            state.latencies_ms.append(elapsed_ms)
            if success:
                state.failures = 0
                state.success_count += 1
                state.last_error = ""
                state.open_until = 0.0
                return
            state.failures += 1
            state.error_count += 1
            if any(marker in error.lower() for marker in ("timeout", "timed out", "deadline", "late")):
                state.timeout_count += 1
            state.last_error = error[:240]
            if state.failures >= self._failure_limit:
                state.open_until = self._monotonic() + self._breaker_seconds

    def _record_source_time(self, source: str, source_time: datetime) -> None:
        with self._state_lock:
            state = self._states[source]
            if state.last_source_time is None or source_time > state.last_source_time:
                state.last_source_time = source_time

    def _record_skipped_open(self, source: str) -> None:
        with self._state_lock:
            state = self._states[source]
            state.error_count += 1
            state.last_error = "circuit_open"


__all__ = ["MarketDataGateway"]
