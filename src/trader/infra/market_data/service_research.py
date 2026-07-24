"""Research cache, fetch and degradation operations for MarketFeatureService."""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, wait
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, ParamSpec, TypedDict, TypeVar, cast

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.application.cache import CacheIdentity
from trader.application.ports.market import MarketDataDeadlineExceededError
from trader.application.workers import BorrowExecutorOptions, WorkerExecutor, borrow_executor, submit_or_run_inline
from trader.domain.market.research import ResearchObservation
from trader.infra.market_data.akshare import AkshareResearchClient
from trader.infra.market_data.service_execution import MarketTaskRunner
from trader.infra.market_data.service_models import _ResearchEntry
from trader.infra.market_data.service_support import (
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
from trader.infra.persistence.runtime_json import RuntimeJsonWriter, atomic_read_json, atomic_write_json

_P = ParamSpec("_P")
_T = TypeVar("_T")


@dataclass(frozen=True)
class ResearchLoaderStatus:
    entries: int
    success_count: int
    error_count: int
    planned_count: int
    timeout_count: int
    consecutive_failures: int
    circuit_open: bool
    latencies_ms: tuple[float, ...]
    latest_source_time: datetime | None
    last_error: str
    out_of_order_count: int
    corporate_risk_covered_count: int
    corporate_risk_fact_count: int
    corporate_risk_registry_versions: tuple[str, ...]


@dataclass(frozen=True)
class _ResearchLoadRequest:
    codes: tuple[str, ...]
    observed_at: datetime
    include_structured: bool
    force: bool
    deadline: datetime | None
    action_restrictions: dict[str, set[str]] | None


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


class _ResearchLoadOptions(_ResearchLoadRequiredOptions, _ResearchLoadOptionalOptions):
    pass


class ResearchLoader:
    def __init__(
        self,
        client: AkshareResearchClient | None,
        runner: MarketTaskRunner,
        **options: Unpack[ResearchLoaderOptions],
    ) -> None:
        self._client = client
        self._runner = runner
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
        request = _ResearchLoadRequest(
            tuple(codes),
            observed_at,
            options["include_structured"],
            options.get("force", False),
            options.get("deadline"),
            options.get("action_restrictions"),
        )
        if self._client is None:
            return {}
        self._runner.ensure_before_deadline(request.deadline)
        source_lanes = self._runner.source_lanes
        if source_lanes is not None and not source_lanes.owns_current_thread("akshare"):
            return self._load_via_source_lane(request)
        return self._load_local(request)

    def _load_via_source_lane(
        self,
        request: _ResearchLoadRequest,
    ) -> Mapping[str, ResearchObservation]:
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
        lane_future = source_lanes.submit(
            "akshare",
            identity,
            request.observed_at,
            self.load,
            request.codes,
            request.observed_at,
            include_structured=request.include_structured,
            force=request.force,
            deadline=request.deadline,
            action_restrictions=request.action_restrictions,
        )
        if request.deadline is None:
            return lane_future.result()
        remaining = max(0.0, (request.deadline - self._runner.wall_clock()).total_seconds())
        try:
            lane_result = lane_future.result(timeout=remaining)
        except FutureTimeoutError as exc:
            lane_future.cancel()
            with self._lock:
                self._error_count += 1
                self._timeout_count += 1
                self._last_error = "research_batch_deadline"
            raise MarketDataDeadlineExceededError("research source lane exceeded its batch deadline") from exc
        self._runner.ensure_before_deadline(request.deadline)
        return lane_result

    def _load_local(
        self,
        request: _ResearchLoadRequest,
    ) -> Mapping[str, ResearchObservation]:
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
        self._load_previous_research(state, now)
        self._mark_loaded_research_actionability(state)
        missing = [code for code in request.codes if request.force or code not in result]
        if not missing:
            self._runner.ensure_before_deadline(request.deadline)
            return result
        with self._lock:
            self._planned_count += len(missing)
        self._fetch_missing_research(state, missing)
        self._trim_research_entries(request)
        return result

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
    ) -> None:
        request = state.request
        source_lanes = self._runner.source_lanes
        with borrow_executor(
            self._runner.worker_pool,
            BorrowExecutorOptions(
                worker_count=min(self._workers, len(missing)),
                thread_name_prefix="candidate-research",
                queue_capacity=len(missing),
                wait_on_exit=request.deadline is None,
                nested_inline=source_lanes is not None and source_lanes.owns_current_thread("akshare"),
            ),
        ) as pool:
            futures, started_at = self._submit_research(pool, state, missing)
            timeout = (
                None
                if request.deadline is None
                else max(0.0, (request.deadline - self._runner.wall_clock()).total_seconds())
            )
            completed, pending = wait(futures, timeout=timeout)
            if pending:
                for future in pending:
                    future.cancel()
                with self._lock:
                    self._error_count += len(pending)
                    self._timeout_count += len(pending)
                    self._last_error = "research_batch_deadline"
                raise MarketDataDeadlineExceededError("research preload exceeded its batch deadline")
            self._runner.ensure_before_deadline(request.deadline)
            for future in completed:
                self._consume_research_future(state, futures[future], future, started_at[future])

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
            future = submit_or_run_inline(
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
    ) -> None:
        request = state.request
        old_entry = state.previous.get(code)
        latency_ms = max(0.0, (self._monotonic() - started_at) * 1000.0)
        observation, ttl = self._resolve_research_result(future, old_entry, latency_ms)
        self._runner.ensure_before_deadline(request.deadline)
        state.result[code] = observation
        self._mark_research_actionability(
            _ResearchActionScope(
                code,
                request.include_structured,
                request.observed_at,
                request.action_restrictions,
            ),
            observation,
        )
        self._runner.ensure_before_deadline(request.deadline)
        self._update_research_memory_cache(code, request.include_structured, observation, request.observed_at)
        self._runner.ensure_before_deadline(request.deadline)
        self._write_research_cache(code, request.include_structured, observation, ttl, state.wall_now)
        self._runner.ensure_before_deadline(request.deadline)
        with self._lock:
            self._runner.ensure_before_deadline(request.deadline)
            self._entries[(code, request.include_structured)] = _ResearchEntry(
                observation,
                self._monotonic() + ttl,
            )

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
            raw = atomic_read_json(path)
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
        return _ResearchEntry(observation, self._monotonic() + remaining_seconds)

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
            writer = self._json_writer.write if self._json_writer is not None else atomic_write_json
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
        self._finish_research_request(success=not observation.source_errors)
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
        with self._lock:
            observations = tuple(entry.observation for entry in self._entries.values())
            return ResearchLoaderStatus(
                entries=len(self._entries),
                success_count=self._success_count,
                error_count=self._error_count,
                planned_count=self._planned_count,
                timeout_count=self._timeout_count,
                consecutive_failures=self._consecutive_failures,
                circuit_open=self._open_until > self._monotonic(),
                latencies_ms=tuple(self._latencies_ms),
                latest_source_time=self._latest_source_time,
                last_error=self._last_error,
                out_of_order_count=self._out_of_order_count,
                corporate_risk_covered_count=sum(
                    observation.corporate_risk_history_complete for observation in observations
                ),
                corporate_risk_fact_count=sum(len(observation.corporate_risk_facts) for observation in observations),
                corporate_risk_registry_versions=tuple(
                    sorted(
                        {
                            observation.corporate_risk_registry_version
                            for observation in observations
                            if observation.corporate_risk_registry_version
                        }
                    )
                ),
            )

    def entries(self) -> Mapping[tuple[str, bool], _ResearchEntry]:
        with self._lock:
            return dict(self._entries)

    @property
    def client(self) -> AkshareResearchClient | None:
        return self._client


__all__ = ["ResearchLoader", "ResearchLoaderStatus"]
