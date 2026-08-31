"""Parallel market-source collection, deterministic merge and source health."""

from __future__ import annotations

import hashlib
import threading
import time
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import TypedDict

from polars.exceptions import PolarsError
from typing_extensions import Unpack

from trader.application.cache import BoundedCache, canonical_json_bytes
from trader.application.ports.market import (
    MarketDataDeadlineExceededError,
    MarketDataFailedError,
    MarketDataNoDataError,
    MarketDataUnavailableError,
)
from trader.application.runtime.latency import LatencyWaterfall
from trader.application.runtime.schedule import shanghai_now
from trader.application.runtime.source_lanes import SourceLaneRegistry, SourceRequestSupersededError
from trader.application.runtime.workers import BoundedExecutor
from trader.domain.market.models import (
    CanonicalMarketSnapshot,
    MarketQuote,
)
from trader.infra.market_data.normalization.columnar import (
    ColumnarQuoteBatch,
    MarketChangeSet,
    market_changes,
    targeted_market_changes,
)
from trader.infra.market_data.normalization.merge import (
    merge_market_observations,
    observation_from_quote,
    overlay_canonical_snapshot,
    snapshot_payload_hash,
)
from trader.infra.market_data.normalization.merge_quote import rejection_reason, source_name
from trader.infra.market_data.providers.eastmoney import EastmoneyClient
from trader.infra.market_data.providers.sina import SinaClient
from trader.infra.market_data.providers.tencent import TencentClient
from trader.infra.market_data.references.security_references import security_reference_observations
from trader.infra.market_data.service.gateway_health import (
    MarketGatewayHealthStatus,
    MarketSourceHealthStatus,
    SecurityMasterHealthStatus,
)
from trader.infra.market_data.service.gateway_runtime import (
    _cache_error_code,
    _CircuitState,
    _cycle_trace_id,
    _elapsed,
    _observation_version,
    _parallel_error_message,
    _parallel_route_outcome,
    _percentile,
    _preserve_newer_quotes,
    _reference_replaces,
    _SingleFlight,
    _source_degraded_reasons,
)
from trader.infra.market_data.service.observations import SourceObservation
from trader.infra.market_data.service.router import RouteOutcome
from trader.infra.market_data.service.source_coordinator import (
    MarketSourceCoordinator,
    MarketSourceDependencies,
    SourceLaneIdentityRequest,
    SourceObservationRequest,
)


class _GatewayRequiredOptions(TypedDict):
    minimum_market_rows: int
    circuit_breaker_failures: int
    circuit_breaker_seconds: int


class _GatewayOptionalOptions(TypedDict, total=False):
    worker_pool: BoundedExecutor | None
    source_lanes: SourceLaneRegistry | None
    cache: BoundedCache[object] | None
    source_contract_versions: Mapping[str, str] | None
    config_version: str
    schema_version: str
    monotonic: Callable[[], float]
    wall_clock: Callable[[], datetime]
    latency: LatencyWaterfall
    full_market_hedge_delay_seconds: float
    listing_open_dates: Callable[[], Sequence[date]]


class _GatewayOptions(_GatewayRequiredOptions, _GatewayOptionalOptions):
    pass


@dataclass(frozen=True)
class _TargetQuoteRequest:
    codes: Sequence[str]
    requested_at: datetime
    force: bool
    deadline: datetime | None
    isolated: bool = False
    dataset: str = "candidate_quotes"
    lane: str = "tencent"
    urgent: bool = False


class MarketDataGateway:
    def __init__(
        self,
        eastmoney: EastmoneyClient,
        sina: SinaClient,
        tencent: TencentClient,
        **options: Unpack[_GatewayOptions],
    ) -> None:
        self._eastmoney = eastmoney
        self._sina = sina
        self._tencent = tencent
        self._minimum_market_rows = options["minimum_market_rows"]
        self._failure_limit = options["circuit_breaker_failures"]
        self._breaker_seconds = options["circuit_breaker_seconds"]
        self._worker_pool = options.get("worker_pool")
        self._source_lanes = options.get("source_lanes")
        self._cache = options.get("cache")
        self._source_contract_versions = dict(
            options.get("source_contract_versions")
            or {
                "eastmoney": "eastmoney-component-v1",
                "sina": "sina-component-v1",
                "tencent": "tencent-component-v1",
            }
        )
        self._source_contract_versions.setdefault(
            "tencent_long",
            self._source_contract_versions.get("tencent", "tencent-component-v1"),
        )
        self._config_version = options.get("config_version", "component-default")
        self._schema_version = options.get("schema_version", "market-v15")
        self._monotonic = options.get("monotonic", time.monotonic)
        self._wall_clock = options.get("wall_clock", lambda: datetime.now(timezone.utc))
        self._latency = options.get("latency") or LatencyWaterfall(monotonic=self._monotonic)
        self._market_flight: _SingleFlight[Sequence[MarketQuote]] = _SingleFlight()
        self._candidate_fetch_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._states = {
            "eastmoney": _CircuitState(),
            "sina": _CircuitState(),
            "tencent": _CircuitState(),
            "tencent_long": _CircuitState(),
        }
        self._recovery_probes = {
            "eastmoney": getattr(eastmoney, "probe_market", None),
            "sina": getattr(sina, "probe_market", None),
        }
        self._latest_by_code: dict[str, MarketQuote] = {}
        self._latest_observations: dict[str, dict[str, SourceObservation]] = {}
        self._reference_observations: dict[str, SourceObservation] = {}
        self._security_reference_persistence_sink: Callable[[Sequence[SourceObservation]], None] | None = None
        self._reference_persistence_schedule_error_count = 0
        self._calendar_open_dates: set[date] = set()
        self._calendar_open_dates_sorted: tuple[date, ...] = ()
        self._listing_open_dates = options.get("listing_open_dates")
        self._listing_open_dates_retry_at = 0.0
        self._latest_snapshot: CanonicalMarketSnapshot | None = None
        self._latest_batch: ColumnarQuoteBatch | None = None
        self._latest_changes = MarketChangeSet("", (), (), ())
        self._latest_source = "unavailable"
        self._last_route_outcome: RouteOutcome | None = None
        self._merge_count = 0
        self._conflict_count = 0
        self._sources = MarketSourceCoordinator(
            MarketSourceDependencies(
                eastmoney=eastmoney,
                sina=sina,
                minimum_market_rows=self._minimum_market_rows,
                worker_pool=self._worker_pool,
                source_lanes=self._source_lanes,
                cache=self._cache,
                source_contract_versions=self._source_contract_versions,
                config_version=self._config_version,
                schema_version=self._schema_version,
                monotonic=self._monotonic,
                wall_clock=self._wall_clock,
                full_market_hedge_delay_seconds=options.get("full_market_hedge_delay_seconds", 1.0),
                full_market_observation_sink=self._promote_full_market_security_references,
            ),
            self,
        )

    def _promote_full_market_security_references(
        self,
        observations: Sequence[SourceObservation],
    ) -> None:
        references = security_reference_observations(observations)
        if not references:
            return
        self.update_reference_observations(references)
        promoted = self.reference_observations(tuple(reference.subject_key for reference in references))
        with self._state_lock:
            persistence_sink = self._security_reference_persistence_sink
        if persistence_sink is None:
            return
        try:
            persistence_sink(promoted)
        except (OSError, RuntimeError, TypeError, ValueError):
            with self._state_lock:
                self._reference_persistence_schedule_error_count += 1

    def set_security_reference_persistence_sink(
        self,
        sink: Callable[[Sequence[SourceObservation]], None],
    ) -> None:
        if not callable(sink):
            raise TypeError("security reference persistence sink must be callable")
        with self._state_lock:
            self._security_reference_persistence_sink = sink

    def fetch_market(
        self,
        *,
        observed_at: datetime | None = None,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[MarketQuote]:
        requested_at = observed_at or self._wall_clock()
        trace_id = _cycle_trace_id("full_market", requested_at, ())
        self._latency.plan(trace_id, "full_market")
        self._latency.enter(trace_id)
        try:
            if self._source_lanes is not None:
                result = self._fetch_market_once(requested_at, force=force, deadline=deadline)
            else:
                result = self._market_flight.run(
                    lambda: self._fetch_market_once(requested_at, force=force, deadline=deadline)
                )
        except MarketDataDeadlineExceededError:
            self._latency.finish(trace_id, outcome="timeout")
            raise
        except SourceRequestSupersededError:
            self._latency.finish(trace_id, outcome="superseded")
            raise
        except BaseException:
            self._latency.finish(trace_id, outcome="failed")
            raise
        self._latency.finish(trace_id, outcome="success")
        return result

    def _fetch_market_once(
        self,
        observed_at: datetime,
        *,
        force: bool,
        deadline: datetime | None,
    ) -> Sequence[MarketQuote]:
        results = self._sources.fetch_market_sources(observed_at, force=force, deadline=deadline)
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
        observed_codes = {observation.subject_key for observation in observations}
        security_references = security_reference_observations(observations)
        with self._state_lock:
            previous = self._latest_snapshot
            self._remember_observations_locked(observations, completed_at)
            self._update_reference_observations_locked(security_references)
            references = tuple(
                observation for code, observation in self._reference_observations.items() if code in observed_codes
            )
        merge_started = self._monotonic()
        snapshot = merge_market_observations(
            (*observations, *references),
            observed_at=completed_at,
            previous=previous,
        )
        self.record_local_latency("merge", _elapsed(merge_started, self._monotonic()))
        snapshot = replace(
            snapshot,
            degraded_reasons=tuple(sorted({*snapshot.degraded_reasons, *_source_degraded_reasons(results)})),
        )
        while True:
            commit_started = self._monotonic()
            with self._state_lock:
                latest = self._latest_snapshot
            commit_snapshot = _preserve_newer_quotes(snapshot, latest)
            commit_snapshot, columnar = _try_columnar_snapshot(
                commit_snapshot,
                config_version=self._config_version,
                schema_version=self._schema_version,
            )
            with self._state_lock:
                if self._latest_snapshot is not latest:
                    continue
                self._latest_snapshot = commit_snapshot
                if columnar is None:
                    self._latest_changes = _columnar_failure_changes(latest, commit_snapshot)
                else:
                    self._latest_changes = market_changes(self._latest_batch, columnar)
                    self._latest_batch = columnar
                self._latest_by_code = {quote.code: quote for quote in commit_snapshot.quotes}
                self._latest_source = "eastmoney+sina" if len(successes) == 2 else outcome.vendor
                self._last_route_outcome = outcome
                self._merge_count += 1
                self._conflict_count += len(commit_snapshot.conflicts)
                self.record_local_latency(
                    "canonical_commit",
                    _elapsed(commit_started, self._monotonic()),
                )
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
        trace_id = _cycle_trace_id("candidate_quotes", requested_at, codes)
        self._latency.plan(trace_id, "candidate_quotes")
        self._latency.enter(trace_id)
        try:
            if self._source_lanes is not None:
                result = self._fetch_candidates_once(_TargetQuoteRequest(codes, requested_at, force, deadline))
            else:
                with self._candidate_fetch_lock:
                    result = self._fetch_candidates_once(_TargetQuoteRequest(codes, requested_at, force, deadline))
        except MarketDataDeadlineExceededError:
            self._latency.finish(trace_id, outcome="timeout")
            raise
        except SourceRequestSupersededError:
            self._latency.finish(trace_id, outcome="superseded")
            raise
        except BaseException:
            self._latency.finish(trace_id, outcome="failed")
            raise
        self._latency.finish(trace_id, outcome="success")
        return result

    def fetch_topk_quotes(
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
        trace_id = _cycle_trace_id("topk_quotes", requested_at, codes)
        self._latency.plan(trace_id, "topk_quotes")
        self._latency.enter(trace_id)
        try:
            result = self._fetch_candidates_once(
                _TargetQuoteRequest(
                    codes,
                    requested_at,
                    force,
                    deadline,
                    dataset="topk_quotes",
                    lane="tencent_topk",
                    urgent=True,
                )
            )
        except BaseException:
            self._latency.finish(trace_id, outcome="failed")
            raise
        self._latency.finish(trace_id, outcome="success")
        return result

    def fetch_long_quotes(
        self,
        codes: Sequence[str],
        *,
        observed_at: datetime | None = None,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[MarketQuote]:
        """Fetch the long watchlist on its caller-owned isolated worker."""

        if not codes:
            return ()
        requested_at = observed_at or self._wall_clock()
        trace_id = _cycle_trace_id("long_quotes", requested_at, codes)
        self._latency.plan(trace_id, "long_quotes")
        self._latency.enter(trace_id)
        try:
            result = self._fetch_candidates_once(
                _TargetQuoteRequest(codes, requested_at, force, deadline, isolated=True, dataset="long_quotes")
            )
        except MarketDataDeadlineExceededError:
            self._latency.finish(trace_id, outcome="timeout")
            raise
        except BaseException:
            self._latency.finish(trace_id, outcome="failed")
            raise
        self._latency.finish(trace_id, outcome="success")
        return result

    def _fetch_candidates_once(
        self,
        request: _TargetQuoteRequest,
    ) -> Sequence[MarketQuote]:
        codes = request.codes
        requested_at = request.requested_at
        deadline = request.deadline
        isolated = request.isolated
        physical_source = "tencent_long" if isolated else "tencent"
        normalized_codes = tuple(sorted(set(codes)))
        try:
            observations = self._target_quote_observations(
                request,
                normalized_codes,
                physical_source=physical_source,
            )
        except FutureTimeoutError:
            self._mark_snapshot_degraded("tencent:late", max(requested_at, self._wall_clock()))
            with self._state_lock:
                return tuple(self._latest_by_code[code] for code in codes if code in self._latest_by_code)
        except SourceRequestSupersededError:
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
        merge_started = self._monotonic()
        snapshot = merge_market_observations(
            (*raw_baseline, *baseline_observations, *observations, *references),
            observed_at=completed_at,
            targeted_codes=codes,
        )
        self.record_local_latency("merge", _elapsed(merge_started, self._monotonic()))
        with self._state_lock:
            return self._commit_candidate_snapshot_locked(observations, snapshot, completed_at, codes)

    def _target_quote_observations(
        self,
        request: _TargetQuoteRequest,
        normalized_codes: tuple[str, ...],
        *,
        physical_source: str,
    ) -> Sequence[SourceObservation]:
        quote_request = {"codes": normalized_codes, "fields": ["realtime_quote"]}
        observation_request = SourceObservationRequest(
            physical_source,
            request.dataset,
            ",".join(normalized_codes),
            quote_request,
            lambda: self._tencent.fetch_quotes(
                normalized_codes,
                timeout_seconds=_remaining_seconds(request.deadline, self._wall_clock),
            ),
            request.requested_at,
            request.force,
            request.deadline,
            1,
            request.isolated,
        )
        if self._source_lanes is None or request.isolated:
            return self._sources.fetch_source_observations(observation_request)
        identity = self._sources.lane_identity(
            SourceLaneIdentityRequest(
                request.dataset,
                "tencent",
                ",".join(normalized_codes),
                quote_request,
                request.requested_at,
                request.force,
                request.deadline,
            )
        )
        submit = self._source_lanes.submit_urgent if request.urgent else self._source_lanes.submit
        lane_future = submit(
            request.lane,
            identity,
            request.requested_at,
            self._sources.fetch_source_observations,
            observation_request,
        )
        if request.deadline is None:
            return lane_future.result()
        remaining = max(0.0, (request.deadline - self._wall_clock()).total_seconds())
        try:
            return lane_future.result(timeout=remaining)
        except FutureTimeoutError:
            lane_future.cancel()
            raise

    def _commit_candidate_snapshot_locked(
        self,
        observations: Sequence[SourceObservation],
        snapshot: CanonicalMarketSnapshot,
        completed_at: datetime,
        codes: Sequence[str],
    ) -> tuple[MarketQuote, ...]:
        commit_started = self._monotonic()
        self._remember_observations_locked(observations, completed_at)
        previous = self._latest_snapshot
        commit_snapshot = overlay_canonical_snapshot(previous, snapshot)
        selected_codes = set(codes)
        self._latest_snapshot = commit_snapshot
        self._latest_changes = targeted_market_changes(previous, commit_snapshot, codes)
        if previous is None:
            self._latest_by_code = {quote.code: quote for quote in commit_snapshot.quotes}
        else:
            for quote in commit_snapshot.quotes:
                if quote.code in selected_codes:
                    self._latest_by_code[quote.code] = quote
        self._merge_count += 1
        self._conflict_count += len(snapshot.conflicts)
        self.record_local_latency(
            "targeted_overlay_commit",
            _elapsed(commit_started, self._monotonic()),
        )
        return tuple(self._latest_by_code[code] for code in codes if code in self._latest_by_code)

    def update_reference_observations(self, observations: Sequence[SourceObservation]) -> None:
        with self._state_lock:
            self._update_reference_observations_locked(observations)

    def reference_observations(self, codes: Sequence[str]) -> tuple[SourceObservation, ...]:
        selected = set(codes)
        if not selected:
            return ()
        with self._state_lock:
            return tuple(observation for code, observation in self._reference_observations.items() if code in selected)

    def _update_reference_observations_locked(
        self,
        observations: Sequence[SourceObservation],
    ) -> None:
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
            self._merge_reference_observation_locked(observation)
        if calendar_changed:
            self._reference_observations = {
                code: self._with_listing_sessions(observation)
                for code, observation in self._reference_observations.items()
            }

    def _merge_reference_observation_locked(self, incoming: SourceObservation) -> None:
        current = self._reference_observations.get(incoming.subject_key)
        if current is None:
            merged = incoming
        else:
            incoming_wins = _reference_replaces(current, incoming)
            winner, supplement = (incoming, current) if incoming_wins else (current, incoming)
            fields = dict(supplement.fields)
            fields.update(winner.fields)
            missing_reasons = dict(supplement.missing_reasons)
            missing_reasons.update(winner.missing_reasons)
            for field, value in fields.items():
                if value is not None:
                    missing_reasons.pop(field, None)
            merged = replace(
                winner,
                fields=fields,
                missing_reasons=missing_reasons,
                payload_hash=hashlib.sha256(canonical_json_bytes(fields)).hexdigest(),
            )
        self._reference_observations[incoming.subject_key] = self._with_listing_sessions(merged)

    def _with_listing_sessions(self, observation: SourceObservation) -> SourceObservation:
        listing_raw = observation.fields.get("listing_date")
        self._load_listing_open_dates_locked()
        open_dates = self._calendar_open_dates_sorted
        if not isinstance(listing_raw, str):
            return observation
        try:
            listing_date = date.fromisoformat(listing_raw)
        except ValueError:
            return observation
        observed_date = shanghai_now(observation.observed_at).date()
        if not open_dates:
            return observation
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

    def _load_listing_open_dates_locked(self) -> None:
        if self._calendar_open_dates_sorted or self._listing_open_dates is None:
            return
        now = self._monotonic()
        if now < self._listing_open_dates_retry_at:
            return
        self._listing_open_dates_retry_at = now + 30.0
        try:
            open_dates = tuple(day for day in self._listing_open_dates() if isinstance(day, date))
        except (OSError, RuntimeError, TypeError, ValueError):
            return
        if not open_dates:
            return
        self._calendar_open_dates.update(open_dates)
        self._calendar_open_dates_sorted = tuple(sorted(self._calendar_open_dates))

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

    def _mark_snapshot_degraded(self, reason: str, _observed_at: datetime) -> None:
        with self._state_lock:
            if self._latest_snapshot is None:
                return
            self._latest_snapshot = replace(
                self._latest_snapshot,
                degraded_reasons=tuple(sorted({*self._latest_snapshot.degraded_reasons, reason})),
            )

    def current_quotes(self, codes: Sequence[str]) -> Sequence[MarketQuote]:
        with self._state_lock:
            return tuple(self._latest_by_code[code] for code in codes if code in self._latest_by_code)

    def health(self) -> MarketGatewayHealthStatus:
        now = self._monotonic()
        measured_at = self._wall_clock()
        with self._state_lock:
            reference_rows = tuple(self._reference_observations.values())
            listing_date_rows = sum(
                isinstance(observation.fields.get("listing_date"), str) for observation in reference_rows
            )
            listing_age_rows = sum(
                isinstance(observation.fields.get("listing_age_sessions"), (int, float))
                and not isinstance(observation.fields.get("listing_age_sessions"), bool)
                for observation in reference_rows
            )
            complete_rows = sum(
                isinstance(observation.fields.get("listing_date"), str)
                and isinstance(observation.fields.get("listing_age_sessions"), (int, float))
                and not isinstance(observation.fields.get("listing_age_sessions"), bool)
                for observation in reference_rows
            )
            return MarketGatewayHealthStatus(
                active_source=self._latest_source,
                cached_rows=len(self._latest_by_code),
                merge_count=self._merge_count,
                conflict_count=self._conflict_count,
                snapshot=self._latest_snapshot,
                changes=self._latest_changes,
                route=self._last_route_outcome,
                source_lanes=self._source_lanes.status() if self._source_lanes is not None else None,
                security_master=SecurityMasterHealthStatus(
                    total_rows=len(reference_rows),
                    listing_date_rows=listing_date_rows,
                    listing_age_rows=listing_age_rows,
                    complete_rows=complete_rows,
                    provider="free_market+production_calendar",
                    tushare_required=False,
                    persistence_schedule_error_count=self._reference_persistence_schedule_error_count,
                ),
                sources={
                    name: MarketSourceHealthStatus(
                        planned_count=state.planned_count,
                        success_count=state.success_count,
                        error_count=state.error_count,
                        timeout_count=state.timeout_count,
                        physical_failure_count=state.physical_failure_count,
                        circuit_skipped_count=state.circuit_skipped_count,
                        superseded_count=state.superseded_count,
                        recovery_probe_count=state.recovery_probe_count,
                        recovery_probe_success_count=state.recovery_probe_success_count,
                        consecutive_failures=state.failures,
                        circuit_open=state.open_until > now,
                        last_latency_ms=round(state.last_latency_ms, 2),
                        p50_latency_ms=_percentile(state.latencies_ms, 0.50),
                        p95_latency_ms=_percentile(state.latencies_ms, 0.95),
                        last_error=state.last_error,
                        data_age_seconds=(
                            max(0.0, (measured_at - state.last_source_time).total_seconds())
                            if state.last_source_time is not None
                            else None
                        ),
                    )
                    for name, state in self._states.items()
                },
                cache=self._cache.status() if self._cache is not None else None,
                latency_waterfall=self._latency.status(),
            )

    def record_planned(self, source: str) -> None:
        with self._state_lock:
            self._states[source].planned_count += 1

    def _is_open(self, source: str) -> bool:
        with self._state_lock:
            return self._states[source].open_until > self._monotonic()

    def fetch_physical(
        self,
        source: str,
        fetcher: Callable[[], Sequence[MarketQuote]],
        minimum_rows: int,
    ) -> tuple[Sequence[MarketQuote], float]:
        if self._is_open(source):
            self._record_skipped_open(source)
            raise MarketDataFailedError(source, "circuit_open")
        self._probe_recovering_source(source)
        started = self._monotonic()
        try:
            quotes = tuple(fetcher())
        except MarketDataNoDataError as exc:
            self.record_local_latency("external_source", _elapsed(started, self._monotonic()))
            self._record(source, False, started, str(exc))
            raise
        except Exception as exc:
            self.record_local_latency("external_source", _elapsed(started, self._monotonic()))
            self._record(source, False, started, str(exc))
            raise MarketDataFailedError(source, str(exc)) from exc
        self.record_local_latency("external_source", _elapsed(started, self._monotonic()))
        if len(quotes) < minimum_rows:
            error = MarketDataNoDataError(f"{source}: only {len(quotes)} market rows")
            self._record(source, False, started, str(error))
            raise error
        return quotes, started

    def record_fetch_result(self, source: str, success: bool, started: float, error: str) -> None:
        self._record(source, success, started, error)

    def record_deadline(self, source: str) -> None:
        self._record(source, False, self._monotonic(), "deadline", physical=False)

    def record_superseded(self, source: str) -> None:
        with self._state_lock:
            self._states[source].superseded_count += 1

    def _record(
        self,
        source: str,
        success: bool,
        started: float,
        error: str,
        *,
        physical: bool = True,
    ) -> None:
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
            state.error_count += 1
            if any(marker in error.lower() for marker in ("timeout", "timed out", "deadline", "late")):
                state.timeout_count += 1
            state.last_error = error[:240]
            if not physical:
                return
            state.failures += 1
            state.physical_failure_count += 1
            if state.failures >= self._failure_limit:
                state.open_until = self._monotonic() + self._breaker_seconds

    def record_source_time(self, source: str, source_time: datetime) -> None:
        with self._state_lock:
            state = self._states[source]
            if state.last_source_time is None or source_time > state.last_source_time:
                state.last_source_time = source_time

    def record_local_latency(self, stage: str, duration_ms: float) -> None:
        self._latency.record_duration(stage, duration_ms)

    def _probe_recovering_source(self, source: str) -> None:
        probe = self._recovery_probes.get(source)
        if not callable(probe):
            return
        now = self._monotonic()
        with self._state_lock:
            state = self._states[source]
            recovering = state.failures >= self._failure_limit and 0.0 < state.open_until <= now
            if not recovering:
                return
            state.recovery_probe_count += 1
        started = self._monotonic()
        try:
            probe()
        except Exception as exc:
            self._record(source, False, started, f"recovery_probe:{exc}")
            raise MarketDataFailedError(source, "recovery_probe_failed") from exc
        with self._state_lock:
            state = self._states[source]
            state.recovery_probe_success_count += 1
            state.failures = 0
            state.open_until = 0.0
            state.last_error = ""

    def _record_skipped_open(self, source: str) -> None:
        with self._state_lock:
            state = self._states[source]
            state.circuit_skipped_count += 1
            state.last_error = "circuit_open"


def _remaining_seconds(
    deadline: datetime | None,
    wall_clock: Callable[[], datetime],
) -> float | None:
    if deadline is None:
        return None
    return max(0.05, (deadline - wall_clock()).total_seconds())


def _try_columnar_snapshot(
    snapshot: CanonicalMarketSnapshot,
    *,
    config_version: str,
    schema_version: str,
) -> tuple[CanonicalMarketSnapshot, ColumnarQuoteBatch | None]:
    try:
        columnar = ColumnarQuoteBatch.from_snapshot(
            snapshot,
            config_version=config_version,
            schema_version=schema_version,
        )
    except (PolarsError, RuntimeError, TypeError, ValueError):
        degraded = replace(
            snapshot,
            degraded_reasons=tuple(sorted({*snapshot.degraded_reasons, "columnar_projection_failed"})),
        )
        return degraded, None
    return snapshot, columnar


def _columnar_failure_changes(
    previous: CanonicalMarketSnapshot | None,
    current: CanonicalMarketSnapshot,
) -> MarketChangeSet:
    previous_quotes = {} if previous is None else {quote.code: quote for quote in previous.quotes}
    current_quotes = {quote.code: quote for quote in current.quotes}
    previous_codes = set(previous_quotes)
    current_codes = set(current_quotes)
    dirty_codes = tuple(sorted(previous_codes | current_codes))
    dimensions = (*previous_quotes.values(), *current_quotes.values())
    return MarketChangeSet(
        merge_epoch=current.merge_epoch,
        inserted_codes=tuple(sorted(current_codes - previous_codes)),
        updated_codes=tuple(sorted(current_codes & previous_codes)),
        removed_codes=tuple(sorted(previous_codes - current_codes)),
        previous_merge_epoch=None if previous is None else previous.merge_epoch,
        dirty_boards=tuple(sorted({quote.board.value for quote in dimensions})),
        dirty_industries=tuple(sorted({quote.industry for quote in dimensions if quote.industry})),
        dirty_field_families=("board", "industry", "quote_identity", "quote_liquidity", "quote_price", "risk"),
        risk_changed_codes=dirty_codes,
        overlay_only=False,
        full_invalidation_reason="columnar_projection_failed",
        content_hash=snapshot_payload_hash(current),
    )


__all__ = ["MarketDataGateway"]
