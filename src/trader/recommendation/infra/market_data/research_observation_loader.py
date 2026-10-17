"""Research cache, fetch and degradation operations for MarketFeatureService."""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, ParamSpec, TypedDict, TypeVar, cast

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.infra.atomic_files.json import AtomicReadJson, AtomicWriteJson, RuntimeJsonWriter
from trader.infra.cache_contracts import CacheIdentity
from trader.infra.workers import (
    InjectedExecutor,
    SubmitOrReject,
    WorkerExecutor,
)
from trader.recommendation.application.pipeline.data_source.input_identity import InputVersionClock
from trader.recommendation.application.ports.market_data import MarketDataDeadlineExceededError
from trader.recommendation.domain.market.research import ResearchObservation
from trader.recommendation.infra.market_data.market_cache_identity import (
    _add_action_restriction,
    _degraded_research_observation,
    _deserialize_research_observation,
    _merge_research_observation,
    _research_data_version,
    _research_is_older,
    _research_source_time,
    _serialize_research_observation,
    _source_batch_identity,
)
from trader.recommendation.infra.market_data.market_feature_cache_entries import _ResearchEntry
from trader.recommendation.infra.market_data.market_task_runner import MarketTaskRunner
from trader.recommendation.infra.market_data.provider_ports import ResearchSource, RiskSnapshotBatchSource
from trader.recommendation.infra.market_data.research_component_persistence import (
    LoaderStatus as _loader_status_from_plane,
)
from trader.recommendation.infra.market_data.research_component_persistence import (
    PersistResearchComponentStatusBatch as _persist_research_component_status_batch_from_plane,
)
from trader.recommendation.infra.market_data.research_component_persistence import (
    PersistResearchComponentStatuses as _persist_research_component_statuses_from_plane,
)
from trader.recommendation.infra.market_data.research_component_persistence import (
    RecoverResearchComponentStatuses as _recover_research_component_statuses_from_plane,
)
from trader.recommendation.infra.market_data.research_component_persistence import (
    _ResearchDataPlane as _ResearchDataPlaneProtocol,
)
from trader.recommendation.infra.market_data.research_load_status import (
    ResearchComponentStatus,
    ResearchLoaderStatus,
    ResearchLoadReport,
)

_P = ParamSpec("_P")
_T = TypeVar("_T")
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResearchLoadRequest:
    codes: tuple[str, ...]
    observed_at: datetime
    include_structured: bool
    force: bool
    deadline: datetime | None
    action_restrictions: dict[str, set[str]] | None
    progress: Callable[[int, int], None] | None


@dataclass
class _ResearchLoadState:
    request: _ResearchLoadRequest
    result: dict[str, ResearchObservation]
    previous: dict[str, _ResearchEntry]
    wall_now: datetime


@dataclass(frozen=True)
class _ResearchActionScope:
    code: str
    include_structured: bool
    observed_at: datetime
    action_restrictions: dict[str, set[str]] | None


@dataclass(frozen=True)
class _RiskBatchSubmission:
    futures: Mapping[Future[Mapping[str, ResearchObservation]], tuple[str, ...]]
    started_at: Mapping[Future[Mapping[str, ResearchObservation]], float]


@dataclass(frozen=True)
class _RiskBatchOutcome:
    changed_codes: tuple[str, ...]
    consumed_codes: tuple[str, ...]
    deadline_reached: bool


class ResearchLoaderOptions(TypedDict):
    workers: int
    ttl_seconds: float
    circuit_breaker_failures: int
    circuit_breaker_seconds: float
    capacity: int
    cache_dir: Path | None
    json_writer: RuntimeJsonWriter | None
    monotonic: Callable[[], float]


class _ResearchLoadRequiredOptions(TypedDict):
    include_structured: bool


class _ResearchLoadOptionalOptions(TypedDict, total=False):
    force: bool
    deadline: datetime | None
    action_restrictions: dict[str, set[str]] | None
    progress: Callable[[int, int], None]


class _ResearchLoadOptions(_ResearchLoadRequiredOptions, _ResearchLoadOptionalOptions):
    pass


class ResearchLoader:
    def __init__(
        self,
        client: ResearchSource | None,
        runner: MarketTaskRunner,
        *,
        data_plane: _ResearchDataPlaneProtocol | None = None,
        risk_batch_client: RiskSnapshotBatchSource | None = None,
        **options: Unpack[ResearchLoaderOptions],
    ) -> None:
        self._versions = InputVersionClock()
        self._client = client
        self._runner = runner
        self._data_plane = data_plane
        self._risk_batch_client = risk_batch_client
        self._workers = max(1, options["workers"])
        self._ttl_seconds = max(60.0, options["ttl_seconds"])
        self._failure_limit = max(1, options["circuit_breaker_failures"])
        self._breaker_seconds = max(0.1, options["circuit_breaker_seconds"])
        self._capacity = max(1, options["capacity"])
        self._cache_dir = options["cache_dir"]
        self._json_writer = options["json_writer"]
        self._monotonic = options["monotonic"]
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, bool], _ResearchEntry] = {}
        self._success_count = 0
        self._error_count = 0
        self._planned_count = 0
        self._timeout_count = 0
        self._consecutive_failures = 0
        self._latencies_ms: deque[float] = deque(maxlen=256)
        self._latest_source_time: datetime | None = None
        self._last_error = ""
        self._open_until = 0.0
        self._half_open_probe = False
        self._out_of_order_count = 0
        self._component_statuses: dict[str, dict[str, ResearchComponentStatus]] = {}

    def cached(
        self,
        codes: Sequence[str],
        *,
        include_structured: bool,
        fresh_only: bool = False,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> Mapping[str, ResearchObservation]:
        now = self._monotonic()
        result, observed_at = self._shared_cached_research(
            codes,
            include_structured=include_structured,
            fresh_only=fresh_only,
            action_restrictions=action_restrictions,
        )
        wall_now = self._runner.wall_clock()
        entries = {
            **self._load_cached_research_from_memory(codes, include_structured, now, result),
            **self._load_cached_research_from_disk(codes, include_structured, wall_now, result),
        }
        self._mark_cached_research_actionability(
            entries,
            include_structured=include_structured,
            observed_at=observed_at or wall_now,
            action_restrictions=action_restrictions,
        )
        return result

    def _load_cached_research_from_memory(
        self,
        codes: Sequence[str],
        include_structured: bool,
        now: float,
        result: dict[str, ResearchObservation],
    ) -> dict[str, _ResearchEntry]:
        entries: dict[str, _ResearchEntry] = {}
        with self._lock:
            for code in codes:
                if code in result:
                    continue
                entry = self._entries.get((code, include_structured))
                if entry is None or entry.expires_at <= now:
                    continue
                result[code] = entry.observation
                entries[code] = entry
        return entries

    def _load_cached_research_from_disk(
        self,
        codes: Sequence[str],
        include_structured: bool,
        wall_now: datetime,
        result: dict[str, ResearchObservation],
    ) -> dict[str, _ResearchEntry]:
        entries: dict[str, _ResearchEntry] = {}
        for code in codes:
            if code in result:
                continue
            entry = self._load_research_cache(code, include_structured, wall_now)
            if entry is None:
                continue
            result[code] = entry.observation
            entries[code] = entry
        if entries:
            with self._lock:
                for code, entry in entries.items():
                    self._entries[(code, include_structured)] = entry
        return entries

    def _mark_cached_research_actionability(
        self,
        entries: Mapping[str, _ResearchEntry],
        *,
        include_structured: bool,
        observed_at: datetime,
        action_restrictions: dict[str, set[str]] | None,
    ) -> None:
        for code, entry in entries.items():
            self._mark_research_actionability(
                _ResearchActionScope(code, include_structured, observed_at, action_restrictions),
                entry.observation,
            )

    def _shared_cached_research(
        self,
        codes: Sequence[str],
        *,
        include_structured: bool,
        fresh_only: bool,
        action_restrictions: dict[str, set[str]] | None,
    ) -> tuple[dict[str, ResearchObservation], datetime | None]:
        result: dict[str, ResearchObservation] = {}
        cache = self._runner.cache
        if cache is None:
            return result, None
        observed_at = self._runner.wall_clock()
        for code in codes:
            identity = self._research_success_identity(code, include_structured, observed_at)
            lookup = cache.get(identity)
            if (
                lookup is not None
                and lookup.value is not None
                and (not fresh_only or lookup.state == "fresh" or lookup.retry_suppressed)
            ):
                result[code] = cast(ResearchObservation, lookup.value)
                if lookup.state == "degraded":
                    _add_action_restriction(action_restrictions, code, "research_data_degraded")
        return result, observed_at

    def load(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        **options: Unpack[_ResearchLoadOptions],
    ) -> Mapping[str, ResearchObservation]:
        report = self.load_report(codes, observed_at, **options)
        if report.deadline_reached and report.deferred_codes:
            raise MarketDataDeadlineExceededError("research preload exceeded its batch deadline")
        return report.observations

    def load_report(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        **options: Unpack[_ResearchLoadOptions],
    ) -> ResearchLoadReport:
        request = _ResearchLoadRequest(
            tuple(codes),
            observed_at,
            options["include_structured"],
            options.get("force", False),
            options.get("deadline"),
            options.get("action_restrictions"),
            options.get("progress"),
        )
        if self._client is None:
            return ResearchLoadReport({})
        try:
            self._runner.ensure_before_deadline(request.deadline)
        except MarketDataDeadlineExceededError:
            return self._deadline_report(request)
        source_lanes = self._runner.source_lanes
        if source_lanes is not None and not source_lanes.owns_current_thread("akshare"):
            return self._load_via_source_lane(request)
        return self._load_local(request)

    def _load_via_source_lane(
        self,
        request: _ResearchLoadRequest,
    ) -> ResearchLoadReport:
        source_lanes = self._runner.source_lanes
        assert source_lanes is not None
        identity = _source_batch_identity(
            "research_success",
            request.codes,
            request.observed_at,
            include_structured=request.include_structured,
            force=request.force,
            deadline=request.deadline,
        )
        if request.progress is None:
            lane_future = source_lanes.submit(
                "akshare",
                identity,
                request.observed_at,
                self.load_report,
                request.codes,
                request.observed_at,
                include_structured=request.include_structured,
                force=request.force,
                deadline=request.deadline,
                action_restrictions=request.action_restrictions,
            )
        else:
            lane_future = source_lanes.submit(
                "akshare",
                identity,
                request.observed_at,
                self.load_report,
                request.codes,
                request.observed_at,
                include_structured=request.include_structured,
                force=request.force,
                deadline=request.deadline,
                action_restrictions=request.action_restrictions,
                progress=request.progress,
            )
        if request.deadline is None:
            return lane_future.result()
        remaining = max(0.0, (request.deadline - self._runner.wall_clock()).total_seconds())
        try:
            lane_result = lane_future.result(timeout=remaining)
        except FutureTimeoutError:
            lane_future.cancel()
            with self._lock:
                self._error_count += 1
                self._timeout_count += 1
                self._last_error = "research_batch_deadline"
            return self._deadline_report(request)
        try:
            self._runner.ensure_before_deadline(request.deadline)
        except MarketDataDeadlineExceededError:
            return ResearchLoadReport(
                lane_result.observations,
                changed_codes=lane_result.changed_codes,
                deferred_codes=lane_result.deferred_codes,
                deadline_reached=True,
            )
        return lane_result

    def _load_local(
        self,
        request: _ResearchLoadRequest,
    ) -> ResearchLoadReport:
        now = self._monotonic()
        wall_now = self._runner.wall_clock()
        result = (
            {}
            if request.force
            else dict(
                self.cached(
                    request.codes,
                    include_structured=request.include_structured,
                    fresh_only=True,
                    action_restrictions=request.action_restrictions,
                )
            )
        )
        state = _ResearchLoadState(request, result, {}, wall_now)
        try:
            self._load_previous_research(state, now)
        except MarketDataDeadlineExceededError:
            return self._deadline_report(request, result)
        self._mark_loaded_research_actionability(state)
        missing = [code for code in request.codes if request.force or code not in result]
        if not missing:
            return ResearchLoadReport(result)
        with self._lock:
            self._planned_count += len(missing)
        changed, deferred = self._fetch_missing_research(state, missing)
        self._trim_research_entries(request)
        return ResearchLoadReport(
            result,
            changed_codes=changed,
            deferred_codes=deferred,
            deadline_reached=bool(deferred),
        )

    def _deadline_report(
        self,
        request: _ResearchLoadRequest,
        observations: Mapping[str, ResearchObservation] | None = None,
    ) -> ResearchLoadReport:
        available = dict(observations or {})
        if not available:
            available.update(
                self.cached(
                    request.codes,
                    include_structured=request.include_structured,
                    action_restrictions=request.action_restrictions,
                )
            )
        deferred = tuple(code for code in request.codes if code not in available)
        with self._lock:
            self._error_count += len(deferred) or 1
            self._timeout_count += len(deferred) or 1
            self._last_error = "research_batch_deadline"
        return ResearchLoadReport(available, deferred_codes=deferred, deadline_reached=True)

    def _load_previous_research(self, state: _ResearchLoadState, now: float) -> None:
        request = state.request
        with self._lock:
            memory = {
                code: entry
                for code in request.codes
                if (entry := self._entries.get((code, request.include_structured))) is not None
            }
        for code, entry in memory.items():
            state.previous[code] = entry
            if not request.force and code not in state.result and entry.expires_at > now:
                state.result[code] = entry.observation
        for code in request.codes:
            if code in state.result:
                continue
            self._runner.ensure_before_deadline(request.deadline)
            cached = self._load_research_cache(code, request.include_structured, state.wall_now)
            self._runner.ensure_before_deadline(request.deadline)
            if cached is not None:
                with self._lock:
                    self._entries[(code, request.include_structured)] = cached
                state.result[code] = cached.observation
                state.previous[code] = cached

    def recover_from_data_plane(
        self,
        observed_at: datetime | None = None,
        *,
        retain_stale: bool = False,
        codes: Sequence[str] | None = None,
        research_only: bool = False,
    ) -> None:
        data_plane = self._data_plane
        if data_plane is None:
            return
        _recover_research_component_statuses_from_plane(
            self,
            data_plane,
            observed_at or self._runner.wall_clock(),
            retain_stale=retain_stale,
            codes=codes,
            research_only=research_only,
        )

    def _mark_loaded_research_actionability(self, state: _ResearchLoadState) -> None:
        for code, observation in state.result.items():
            self._mark_research_actionability(
                _ResearchActionScope(
                    code,
                    state.request.include_structured,
                    state.wall_now,
                    state.request.action_restrictions,
                ),
                observation,
            )

    def _fetch_missing_research(
        self,
        state: _ResearchLoadState,
        missing: Sequence[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        request = state.request
        source_lanes = self._runner.source_lanes
        pool = InjectedExecutor(
            self._runner.worker_pool,
            inline=source_lanes is not None and source_lanes.owns_current_thread("akshare"),
        )
        if request.include_structured and not request.force and self._risk_batch_client is not None:
            return self._fetch_missing_risk_batches(pool, state, missing)
        futures, started_at = self._submit_research(pool, state, missing)
        timeout = self._remaining_research_timeout(request.deadline)
        pending = set(futures)
        consumed: set[str] = set()
        changed_codes: list[str] = []
        deadline_reached = False
        while pending:
            completed, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            if not completed:
                break
            completed_by_code = {futures[future]: future for future in completed}
            for code in missing:
                future = completed_by_code.get(code)
                if future is None:
                    continue
                try:
                    self._runner.ensure_before_deadline(request.deadline)
                except MarketDataDeadlineExceededError:
                    deadline_reached = True
                    break
                if self._consume_research_future(state, code, future, started_at[future]):
                    changed_codes.append(code)
                consumed.add(code)
                if request.progress is not None:
                    request.progress(len(consumed), len(missing))
            if deadline_reached:
                break
            timeout = self._remaining_research_timeout(request.deadline)
        deferred = tuple(code for code in missing if code not in consumed)
        self._cancel_deferred_research(futures, consumed, deferred)
        return tuple(changed_codes), deferred

    def _fetch_missing_risk_batches(
        self,
        pool: WorkerExecutor,
        state: _ResearchLoadState,
        missing: Sequence[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        client = self._risk_batch_client
        assert client is not None
        submission = self._submit_risk_batches(pool, state, missing, client)
        pending = set(submission.futures)
        consumed: set[str] = set()
        changed_codes: list[str] = []
        timeout = self._remaining_research_timeout(state.request.deadline)
        while pending:
            completed, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            if not completed:
                break
            deadline_reached = False
            for future in sorted(completed, key=lambda item: submission.futures[item]):
                outcome = self._consume_risk_batch(
                    state,
                    future,
                    submission.futures[future],
                    submission.started_at[future],
                )
                changed_codes.extend(outcome.changed_codes)
                consumed.update(outcome.consumed_codes)
                if state.request.progress is not None:
                    state.request.progress(len(consumed), len(missing))
                deadline_reached = outcome.deadline_reached
                if deadline_reached:
                    break
            if deadline_reached:
                break
            timeout = self._remaining_research_timeout(state.request.deadline)

        deferred = tuple(code for code in missing if code not in consumed)
        self._cancel_deferred_risk_batches(submission.futures, consumed, deferred)
        return tuple(changed_codes), deferred

    def _submit_risk_batches(
        self,
        pool: WorkerExecutor,
        state: _ResearchLoadState,
        missing: Sequence[str],
        client: RiskSnapshotBatchSource,
    ) -> _RiskBatchSubmission:
        batches = tuple(tuple(missing[index : index + 50]) for index in range(0, len(missing), 50))
        futures: dict[Future[Mapping[str, ResearchObservation]], tuple[str, ...]] = {}
        started_at: dict[Future[Mapping[str, ResearchObservation]], float] = {}
        for batch in batches:
            self._runner.ensure_before_deadline(state.request.deadline)
            started = self._monotonic()
            future = SubmitOrReject(
                pool,
                client.fetch_risk_snapshots,
                batch,
                observed_at=state.request.observed_at,
            )
            futures[future] = batch
            started_at[future] = started
        return _RiskBatchSubmission(futures=futures, started_at=started_at)

    def _consume_risk_batch(
        self,
        state: _ResearchLoadState,
        future: Future[Mapping[str, ResearchObservation]],
        batch: tuple[str, ...],
        started_at: float,
    ) -> _RiskBatchOutcome:
        changed_codes: list[str] = []
        deadline_reached = False
        batch_observations: dict[str, ResearchObservation] = {}
        latency_ms = max(0.0, (self._monotonic() - started_at) * 1000.0)
        try:
            observations = future.result()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            observations = {
                code: ResearchObservation(source_errors=(f"batch:{_batch_error_code(exc)}",)) for code in batch
            }
        consumed: list[str] = []
        for code in batch:
            try:
                self._runner.ensure_before_deadline(state.request.deadline)
            except MarketDataDeadlineExceededError:
                deadline_reached = True
                break
            observation = observations.get(code)
            if observation is None:
                observation = ResearchObservation(source_errors=("batch:source_failed",))
            if self._consume_research_observation(state, code, observation, latency_ms, persist=False):
                changed_codes.append(code)
            batch_observations[code] = state.result[code]
            consumed.append(code)
        if batch_observations and self._data_plane is not None:
            _persist_research_component_status_batch_from_plane(
                self,
                self._data_plane,
                state.request.observed_at,
                batch_observations,
            )
        return _RiskBatchOutcome(tuple(changed_codes), tuple(consumed), deadline_reached)

    def _cancel_deferred_risk_batches(
        self,
        futures: Mapping[Future[Mapping[str, ResearchObservation]], tuple[str, ...]],
        consumed: set[str],
        deferred: tuple[str, ...],
    ) -> None:
        if deferred:
            for future, batch in futures.items():
                if any(code not in consumed for code in batch):
                    future.cancel()
            with self._lock:
                self._error_count += len(deferred)
                self._timeout_count += len(deferred)
                self._last_error = "research_batch_deadline"

    def _cancel_deferred_research(
        self,
        futures: Mapping[Future[ResearchObservation], str],
        consumed: set[str],
        deferred: tuple[str, ...],
    ) -> None:
        if not deferred:
            return
        for future, code in futures.items():
            if code not in consumed:
                future.cancel()
        with self._lock:
            self._error_count += len(deferred)
            self._timeout_count += len(deferred)
            self._last_error = "research_batch_deadline"

    def _remaining_research_timeout(self, deadline: datetime | None) -> float | None:
        if deadline is None:
            return None
        return max(0.0, (deadline - self._runner.wall_clock()).total_seconds())

    def _submit_research(
        self,
        pool: WorkerExecutor,
        state: _ResearchLoadState,
        missing: Sequence[str],
    ) -> tuple[
        dict[Future[ResearchObservation], str],
        dict[Future[ResearchObservation], float],
    ]:
        futures: dict[Future[ResearchObservation], str] = {}
        started_at: dict[Future[ResearchObservation], float] = {}
        for code in missing:
            self._runner.ensure_before_deadline(state.request.deadline)
            started = self._monotonic()
            future = SubmitOrReject(
                pool,
                self._fetch_research_observation,
                code,
                state.request.observed_at,
                include_structured=state.request.include_structured,
            )
            self._runner.ensure_before_deadline(state.request.deadline)
            futures[future] = code
            started_at[future] = started
        return futures, started_at

    def _consume_research_future(
        self,
        state: _ResearchLoadState,
        code: str,
        future: Future[ResearchObservation],
        started_at: float,
    ) -> bool:
        old_entry = state.previous.get(code)
        latency_ms = max(0.0, (self._monotonic() - started_at) * 1000.0)
        observation, ttl = self._resolve_research_result(future, old_entry, latency_ms)
        return self._commit_research_observation(state, code, observation, ttl)

    def _consume_research_observation(
        self,
        state: _ResearchLoadState,
        code: str,
        observation: ResearchObservation,
        latency_ms: float,
        *,
        persist: bool = True,
    ) -> bool:
        old_entry = state.previous.get(code)
        observation, ttl = self._resolve_research_observation(observation, old_entry, latency_ms)
        return self._commit_research_observation(state, code, observation, ttl, persist=persist)

    def _commit_research_observation(
        self,
        state: _ResearchLoadState,
        code: str,
        observation: ResearchObservation,
        ttl: float,
        *,
        persist: bool = True,
    ) -> bool:
        request = state.request
        old_entry = state.previous.get(code)
        changed = old_entry is None or _research_facts(old_entry.observation) != _research_facts(observation)
        observation = replace(
            observation,
            data_version=old_entry.observation.data_version
            if old_entry is not None and not changed
            else self._versions.advance("research"),
        )
        state.result[code] = observation
        if persist and request.include_structured:
            if self._data_plane is not None:
                _persist_research_component_statuses_from_plane(
                    self,
                    self._data_plane,
                    code,
                    request.observed_at,
                    observation,
                )
        self._mark_research_actionability(
            _ResearchActionScope(
                code,
                request.include_structured,
                request.observed_at,
                request.action_restrictions,
            ),
            observation,
        )
        self._update_research_memory_cache(code, request.include_structured, observation, request.observed_at)
        self._write_research_cache(code, request.include_structured, observation, ttl, state.wall_now)
        with self._lock:
            self._entries[(code, request.include_structured)] = _ResearchEntry(
                observation,
                self._monotonic() + ttl,
            )
        return changed

    def _resolve_research_result(
        self,
        future: Future[ResearchObservation],
        old_entry: _ResearchEntry | None,
        latency_ms: float,
    ) -> tuple[ResearchObservation, float]:
        ttl = self._ttl_seconds
        try:
            observation = future.result()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            observation = _degraded_research_observation(old_entry, str(exc))
            with self._lock:
                self._error_count += 1
                self._latencies_ms.append(latency_ms)
                self._last_error = str(exc)[:240]
            return observation, min(60.0, ttl)
        return self._resolve_research_observation(observation, old_entry, latency_ms)

    def _resolve_research_observation(
        self,
        observation: ResearchObservation,
        old_entry: _ResearchEntry | None,
        latency_ms: float,
    ) -> tuple[ResearchObservation, float]:
        ttl = self._ttl_seconds
        if _research_is_older(observation, old_entry):
            observation = _degraded_research_observation(old_entry, "out_of_order_research_result")
            ttl = min(60.0, ttl)
            with self._lock:
                self._out_of_order_count += 1
        else:
            observation = _merge_research_observation(old_entry, observation)
        return observation, self._record_research_success(observation, latency_ms, ttl)

    def _record_research_success(
        self,
        observation: ResearchObservation,
        latency_ms: float,
        ttl: float,
    ) -> float:
        with self._lock:
            self._success_count += 1
            self._latencies_ms.append(latency_ms)
            source_time = _research_source_time(observation)
            if source_time is not None and (self._latest_source_time is None or source_time > self._latest_source_time):
                self._latest_source_time = source_time
            if observation.source_errors:
                self._error_count += len(observation.source_errors)
                self._last_error = observation.source_errors[-1][:240]
                return min(60.0, ttl)
        return ttl

    def _trim_research_entries(self, request: _ResearchLoadRequest) -> None:
        with self._lock:
            excess = len(self._entries) - self._capacity
            if excess <= 0:
                return
            requested = {(code, request.include_structured) for code in request.codes}
            victims = sorted(
                self._entries,
                key=lambda key: (key in requested, self._entries[key].expires_at, key),
            )[:excess]
            for key in victims:
                self._entries.pop(key, None)

    def _load_research_cache(
        self,
        code: str,
        include_structured: bool,
        wall_now: datetime,
    ) -> _ResearchEntry | None:
        if self._cache_dir is None:
            return None
        path = self._research_cache_path(code, include_structured)
        try:
            raw = AtomicReadJson(path)
            if not isinstance(raw, Mapping):
                raise TypeError("research cache root must be a mapping")
            observation_raw = raw["observation"]
            expires_at_raw = raw["expires_at"]
            if not isinstance(observation_raw, Mapping) or not isinstance(expires_at_raw, str):
                raise TypeError("research cache fields have invalid types")
            expires_at = datetime.fromisoformat(expires_at_raw)
            if expires_at.tzinfo is None:
                raise ValueError("research cache expiry must be timezone-aware")
            remaining_seconds = (expires_at - wall_now).total_seconds()
            if remaining_seconds <= 0:
                raise ValueError("research cache has expired")
            observation = _deserialize_research_observation(observation_raw)
        except (KeyError, OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        return _ResearchEntry(
            replace(observation, data_version=self._versions.advance("research")), self._monotonic() + remaining_seconds
        )

    def _mark_research_actionability(
        self,
        scope: _ResearchActionScope,
        observation: ResearchObservation,
    ) -> None:
        cache = self._runner.cache
        source_time = _research_source_time(observation)
        if cache is None or source_time is None:
            return
        identity = self._research_success_identity(scope.code, scope.include_structured, scope.observed_at)
        if not cache.is_actionable(identity, source_time):
            _add_action_restriction(scope.action_restrictions, scope.code, "research_data_degraded")

    def _research_success_identity(
        self,
        code: str,
        include_structured: bool,
        observed_at: datetime,
    ) -> CacheIdentity:
        return self._runner.cache_identity(
            "research_success",
            "akshare",
            code,
            {"code": code, "include_structured": include_structured},
            observed_at,
        )

    def _update_research_memory_cache(
        self,
        code: str,
        include_structured: bool,
        observation: ResearchObservation,
        observed_at: datetime,
    ) -> None:
        cache = self._runner.cache
        if cache is None:
            return
        success_identity = self._research_success_identity(code, include_structured, observed_at)
        source_time = _research_source_time(observation) or observed_at
        cache.put(
            success_identity,
            observation,
            data_version=_research_data_version(observation),
            source_time=source_time,
        )
        if not observation.source_errors:
            return
        cache.put_negative(success_identity, error_code="research_refresh_failed")
        failure_identity = self._runner.cache_identity(
            "research_failure",
            "akshare",
            code,
            {"code": code, "include_structured": include_structured},
            observed_at,
        )
        cache.put_negative(failure_identity, error_code="research_refresh_failed")

    def _write_research_cache(
        self,
        code: str,
        include_structured: bool,
        observation: ResearchObservation,
        ttl: float,
        wall_now: datetime,
    ) -> None:
        if self._cache_dir is None:
            return
        target = self._research_cache_path(code, include_structured)
        expires_at = wall_now + timedelta(seconds=ttl)
        try:
            writer = self._json_writer.write if self._json_writer is not None else AtomicWriteJson
            writer(
                target,
                {
                    "code": code,
                    "include_structured": include_structured,
                    "expires_at": expires_at.isoformat(),
                    "observation": _serialize_research_observation(observation),
                },
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            with self._lock:
                self._error_count += 1
                self._last_error = f"research_cache_write_failed:{type(exc).__name__}"

    def _research_cache_path(self, code: str, include_structured: bool) -> Path:
        assert self._cache_dir is not None
        scope = "structured" if include_structured else "news"
        return self._cache_dir / "observations" / scope / f"{code}.json"

    def _fetch_research_observation(
        self,
        code: str,
        observed_at: datetime,
        *,
        include_structured: bool,
    ) -> ResearchObservation:
        if self._client is None:
            return ResearchObservation()
        if not self._begin_research_request():
            raise RuntimeError("akshare_circuit_open")
        try:
            if include_structured:
                observation = self._client.fetch_snapshot(code, observed_at=observed_at)
            else:
                observation = ResearchObservation(
                    evidence=tuple(self._client.fetch_news(code, observed_at=observed_at))
                )
        except Exception:
            self._finish_research_request(success=False)
            raise
        # A returned observation is a successful adapter call even when one component
        # degraded. Component errors retain their own retry state and must not open the
        # shared circuit for unrelated stocks and endpoints.
        self._finish_research_request(success=True)
        return observation

    def _begin_research_request(self) -> bool:
        with self._lock:
            now = self._monotonic()
            if self._open_until > now or self._half_open_probe:
                self._last_error = "akshare_circuit_open"
                return False
            if self._open_until > 0.0:
                self._half_open_probe = True
            return True

    def _finish_research_request(self, *, success: bool) -> None:
        with self._lock:
            self._half_open_probe = False
            if success:
                self._consecutive_failures = 0
                self._open_until = 0.0
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_limit:
                self._open_until = self._monotonic() + self._breaker_seconds

    def status(self) -> ResearchLoaderStatus:
        return _loader_status_from_plane(self)

    def entries(self) -> Mapping[tuple[str, bool], _ResearchEntry]:
        with self._lock:
            return dict(self._entries)

    @property
    def client(self) -> ResearchSource | None:
        return self._client


def _batch_error_code(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    if "timeout" in name or "deadline" in str(exc).lower():
        return "timeout"
    return "source_failed"


def _research_facts(observation: ResearchObservation) -> ResearchObservation:
    return replace(
        observation,
        data_version="research:unassigned",
        evidence=tuple(replace(item, received_at=item.published_at) for item in observation.evidence),
    )


__all__ = ["ResearchLoader"]
