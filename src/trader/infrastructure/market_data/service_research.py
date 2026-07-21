"""Research cache, fetch and degradation operations for MarketFeatureService."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import wait
from datetime import datetime, timedelta
from pathlib import Path
from typing import ParamSpec, TypeVar, cast

from trader.application.ports import MarketDataDeadlineExceeded
from trader.application.workers import borrow_executor, submit_or_run_inline
from trader.domain.research import ResearchObservation
from trader.domain.tail import MinuteBar
from trader.infrastructure.market_data.service_models import _ResearchEntry
from trader.infrastructure.market_data.service_state import MarketServiceState
from trader.infrastructure.market_data.service_support import (
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
from trader.infrastructure.persistence.runtime_json import atomic_read_json, atomic_write_json

_P = ParamSpec("_P")
_T = TypeVar("_T")


class MarketResearchMixin(MarketServiceState):
    def _cached_research(
        self,
        codes: Sequence[str],
        *,
        include_structured: bool,
        fresh_only: bool = False,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> Mapping[str, ResearchObservation]:
        now = self._monotonic()
        result: dict[str, ResearchObservation] = {}
        cache = self._cache
        if cache is not None:
            observed_at = self._wall_clock()
            for code in codes:
                identity = self._data_cache_identity(
                    "research_success",
                    "akshare",
                    code,
                    {"code": code, "include_structured": include_structured},
                    observed_at,
                )
                lookup = cache.get(identity)
                if (
                    lookup is not None
                    and lookup.value is not None
                    and (not fresh_only or lookup.state == "fresh" or lookup.retry_suppressed)
                ):
                    result[code] = cast(ResearchObservation, lookup.value)
                    if lookup.state == "degraded":
                        _add_action_restriction(action_restrictions, code, "research_data_degraded")
        with self._lock:
            for code in codes:
                if code in result:
                    continue
                entry = self._research.get((code, include_structured))
                if entry is None:
                    continue
                if entry.expires_at <= now:
                    continue
                result[code] = entry.observation
                if cache is not None:
                    source_time = _research_source_time(entry.observation)
                    if source_time is not None:
                        identity = self._data_cache_identity(
                            "research_success",
                            "akshare",
                            code,
                            {"code": code, "include_structured": include_structured},
                            observed_at,
                        )
                        if not cache.is_actionable(identity, source_time):
                            _add_action_restriction(action_restrictions, code, "research_data_degraded")
            return result

    def _cached_intraday(
        self,
        codes: Sequence[str],
        *,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> Mapping[str, tuple[MinuteBar, ...]]:
        now = self._monotonic()
        result: dict[str, tuple[MinuteBar, ...]] = {}
        cache = self._cache
        if cache is not None:
            observed_at = self._wall_clock()
            for code in codes:
                identity = self._data_cache_identity(
                    "intraday_minutes",
                    "eastmoney",
                    code,
                    {"code": code, "scale_minutes": 1, "adjust": "none"},
                    observed_at,
                )
                lookup = cache.get(identity)
                if lookup is not None and lookup.value is not None:
                    result[code] = cast(tuple[MinuteBar, ...], lookup.value)
                    if lookup.state == "degraded":
                        _add_action_restriction(action_restrictions, code, "intraday_data_degraded")
        with self._lock:
            for code in codes:
                if code in result:
                    continue
                entry = self._intraday.get(code)
                if entry is None:
                    continue
                if entry.expires_at <= now:
                    continue
                result[code] = entry.bars
                if cache is not None and entry.bars:
                    identity = self._data_cache_identity(
                        "intraday_minutes",
                        "eastmoney",
                        code,
                        {"code": code, "scale_minutes": 1, "adjust": "none"},
                        observed_at,
                    )
                    if not cache.is_actionable(identity, max(bar.source_time for bar in entry.bars)):
                        _add_action_restriction(action_restrictions, code, "intraday_data_degraded")
            return result

    def _load_research(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_structured: bool,
        force: bool = False,
        deadline: datetime | None = None,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> Mapping[str, ResearchObservation]:
        if self._research_client is None:
            return {}
        self._ensure_before_deadline(deadline)
        source_lanes = self._source_lanes
        if source_lanes is not None and not source_lanes.owns_current_thread("akshare"):
            identity = _source_batch_identity(
                "research_success",
                codes,
                observed_at,
                include_structured=include_structured,
                force=force,
                deadline=deadline,
            )
            lane_future = source_lanes.submit(
                "akshare",
                identity,
                observed_at,
                self._load_research,
                codes,
                observed_at,
                include_structured=include_structured,
                force=force,
                deadline=deadline,
                action_restrictions=action_restrictions,
            )
            if deadline is None:
                return lane_future.result()
            remaining = max(0.0, (deadline - self._wall_clock()).total_seconds())
            try:
                lane_result = lane_future.result(timeout=remaining)
            except FutureTimeoutError as exc:
                lane_future.cancel()
                with self._lock:
                    self._research_error_count += 1
                    self._research_timeout_count += 1
                    self._research_last_error = "research_batch_deadline"
                raise MarketDataDeadlineExceeded("research source lane exceeded its batch deadline") from exc
            self._ensure_before_deadline(deadline)
            return lane_result
        now = self._monotonic()
        wall_now = self._wall_clock()
        result = (
            {}
            if force
            else dict(
                self._cached_research(
                    codes,
                    include_structured=include_structured,
                    fresh_only=True,
                    action_restrictions=action_restrictions,
                )
            )
        )
        previous: dict[str, _ResearchEntry] = {}
        with self._lock:
            for code in codes:
                entry = self._research.get((code, include_structured))
                if entry is None:
                    continue
                previous[code] = entry
                if not force and code not in result and entry.expires_at > now:
                    result[code] = entry.observation
            for code in codes:
                if code in result:
                    continue
                self._ensure_before_deadline(deadline)
                cached = self._load_research_cache(code, include_structured, wall_now)
                self._ensure_before_deadline(deadline)
                if cached is not None:
                    self._research[(code, include_structured)] = cached
                    result[code] = cached.observation
                    previous[code] = cached
        cache = self._cache
        if cache is not None:
            for code, observation in result.items():
                source_time = _research_source_time(observation)
                if source_time is None:
                    continue
                cache_identity = self._data_cache_identity(
                    "research_success",
                    "akshare",
                    code,
                    {"code": code, "include_structured": include_structured},
                    wall_now,
                )
                if not cache.is_actionable(cache_identity, source_time):
                    _add_action_restriction(action_restrictions, code, "research_data_degraded")
        missing = [code for code in codes if force or code not in result]
        if not missing:
            self._ensure_before_deadline(deadline)
            return result
        with self._lock:
            self._research_planned_count += len(missing)
        with borrow_executor(
            self._worker_pool,
            worker_count=min(self._research_workers, len(missing)),
            thread_name_prefix="candidate-research",
            queue_capacity=len(missing),
            wait_on_exit=deadline is None,
            nested_inline=source_lanes is not None and source_lanes.owns_current_thread("akshare"),
        ) as pool:
            futures = {}
            started_at: dict[object, float] = {}
            for code in missing:
                self._ensure_before_deadline(deadline)
                started = self._monotonic()
                future = submit_or_run_inline(
                    pool,
                    self._fetch_research_observation,
                    code,
                    observed_at,
                    include_structured=include_structured,
                )
                self._ensure_before_deadline(deadline)
                futures[future] = code
                started_at[future] = started
            timeout = None if deadline is None else max(0.0, (deadline - self._wall_clock()).total_seconds())
            completed, pending = wait(futures, timeout=timeout)
            if pending:
                for future in pending:
                    future.cancel()
                with self._lock:
                    self._research_error_count += len(pending)
                    self._research_timeout_count += len(pending)
                    self._research_last_error = "research_batch_deadline"
                raise MarketDataDeadlineExceeded("research preload exceeded its batch deadline")
            self._ensure_before_deadline(deadline)
            for future in completed:
                self._ensure_before_deadline(deadline)
                code = futures[future]
                ttl = self._research_ttl_seconds
                old_entry = previous.get(code)
                latency_ms = max(0.0, (self._monotonic() - started_at[future]) * 1000.0)
                try:
                    observation = future.result()
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    observation = _degraded_research_observation(old_entry, str(exc))
                    ttl = min(60.0, ttl)
                    with self._lock:
                        self._research_error_count += 1
                        self._research_latencies_ms.append(latency_ms)
                        self._research_last_error = str(exc)[:240]
                else:
                    if _research_is_older(observation, old_entry):
                        observation = _degraded_research_observation(old_entry, "out_of_order_research_result")
                        ttl = min(60.0, ttl)
                        with self._lock:
                            self._research_out_of_order_count += 1
                    else:
                        observation = _merge_research_observation(old_entry, observation)
                    with self._lock:
                        self._research_success_count += 1
                        self._research_latencies_ms.append(latency_ms)
                        source_time = _research_source_time(observation)
                        if source_time is not None and (
                            self._research_latest_source_time is None or source_time > self._research_latest_source_time
                        ):
                            self._research_latest_source_time = source_time
                        if observation.source_errors:
                            self._research_error_count += len(observation.source_errors)
                            self._research_last_error = observation.source_errors[-1][:240]
                            ttl = min(60.0, ttl)
                self._ensure_before_deadline(deadline)
                result[code] = observation
                source_time = _research_source_time(observation)
                cache = self._cache
                if cache is not None and source_time is not None:
                    cache_identity = self._data_cache_identity(
                        "research_success",
                        "akshare",
                        code,
                        {"code": code, "include_structured": include_structured},
                        observed_at,
                    )
                    if not cache.is_actionable(cache_identity, source_time):
                        _add_action_restriction(action_restrictions, code, "research_data_degraded")
                self._ensure_before_deadline(deadline)
                self._update_research_memory_cache(
                    code,
                    include_structured,
                    observation,
                    observed_at,
                )
                self._ensure_before_deadline(deadline)
                self._write_research_cache(code, include_structured, observation, ttl, wall_now)
                self._ensure_before_deadline(deadline)
                with self._lock:
                    self._ensure_before_deadline(deadline)
                    self._research[(code, include_structured)] = _ResearchEntry(
                        observation,
                        self._monotonic() + ttl,
                    )
            with self._lock:
                excess = len(self._research) - self._research_cache_limit
                if excess > 0:
                    requested = {(code, include_structured) for code in codes}
                    victims = sorted(
                        self._research,
                        key=lambda key: (key in requested, self._research[key].expires_at, key),
                    )[:excess]
                    for key in victims:
                        self._research.pop(key, None)
        return result

    def _load_research_cache(
        self,
        code: str,
        include_structured: bool,
        wall_now: datetime,
    ) -> _ResearchEntry | None:
        if self._research_cache_dir is None:
            return None
        path = self._research_cache_path(code, include_structured)
        try:
            raw = atomic_read_json(path)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(raw, Mapping):
            return None
        observation_raw = raw.get("observation")
        if not isinstance(observation_raw, Mapping):
            return None
        expires_at_raw = raw.get("expires_at")
        if not isinstance(expires_at_raw, str):
            return None
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except ValueError:
            return None
        if expires_at.tzinfo is None:
            return None
        remaining_seconds = (expires_at - wall_now).total_seconds()
        if remaining_seconds <= 0:
            return None
        try:
            observation = _deserialize_research_observation(observation_raw)
        except (ValueError, TypeError):
            return None
        return _ResearchEntry(observation, self._monotonic() + remaining_seconds)

    def _update_research_memory_cache(
        self,
        code: str,
        include_structured: bool,
        observation: ResearchObservation,
        observed_at: datetime,
    ) -> None:
        cache = self._cache
        if cache is None:
            return
        success_identity = self._data_cache_identity(
            "research_success",
            "akshare",
            code,
            {"code": code, "include_structured": include_structured},
            observed_at,
        )
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
        failure_identity = self._data_cache_identity(
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
        if self._research_cache_dir is None:
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
                self._research_error_count += 1
                self._research_last_error = f"research_cache_write_failed:{type(exc).__name__}"

    def _research_cache_path(self, code: str, include_structured: bool) -> Path:
        assert self._research_cache_dir is not None
        scope = "structured" if include_structured else "news"
        return self._research_cache_dir / "observations" / scope / f"{code}.json"

    def _fetch_research_observation(
        self,
        code: str,
        observed_at: datetime,
        *,
        include_structured: bool,
    ) -> ResearchObservation:
        if self._research_client is None:
            return ResearchObservation()
        if not self._begin_research_request():
            raise RuntimeError("akshare_circuit_open")
        try:
            if include_structured:
                observation = self._research_client.fetch_snapshot(code, observed_at=observed_at)
            else:
                observation = ResearchObservation(
                    evidence=tuple(self._research_client.fetch_news(code, observed_at=observed_at))
                )
        except Exception:
            self._finish_research_request(success=False)
            raise
        self._finish_research_request(success=not observation.source_errors)
        return observation

    def _begin_research_request(self) -> bool:
        with self._lock:
            now = self._monotonic()
            if self._research_open_until > now or self._research_half_open_probe:
                self._research_last_error = "akshare_circuit_open"
                return False
            if self._research_open_until > 0.0:
                self._research_half_open_probe = True
            return True

    def _finish_research_request(self, *, success: bool) -> None:
        with self._lock:
            self._research_half_open_probe = False
            if success:
                self._research_consecutive_failures = 0
                self._research_open_until = 0.0
                return
            self._research_consecutive_failures += 1
            if self._research_consecutive_failures >= self._research_failure_limit:
                self._research_open_until = self._monotonic() + self._research_breaker_seconds
