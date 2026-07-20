"""Research cache, fetch and degradation operations for MarketFeatureService."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from concurrent.futures import wait
from datetime import datetime, timedelta
from pathlib import Path
from typing import ParamSpec, TypeVar

from trader.application.workers import borrow_executor
from trader.domain.research import ResearchObservation
from trader.domain.tail import MinuteBar
from trader.infrastructure.market_data.service_models import _ResearchEntry
from trader.infrastructure.market_data.service_state import MarketServiceState
from trader.infrastructure.market_data.service_support import (
    _degraded_research_observation,
    _deserialize_research_observation,
    _merge_research_observation,
    _research_is_older,
    _serialize_research_observation,
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
    ) -> Mapping[str, ResearchObservation]:
        now = self._monotonic()
        with self._lock:
            result: dict[str, ResearchObservation] = {}
            for code in codes:
                entry = self._research.get((code, include_structured))
                if entry is None:
                    continue
                if entry.expires_at <= now:
                    continue
                result[code] = entry.observation
            return result

    def _cached_intraday(self, codes: Sequence[str]) -> Mapping[str, tuple[MinuteBar, ...]]:
        now = self._monotonic()
        with self._lock:
            result: dict[str, tuple[MinuteBar, ...]] = {}
            for code in codes:
                entry = self._intraday.get(code)
                if entry is None:
                    continue
                if entry.expires_at <= now:
                    continue
                result[code] = entry.bars
            return result

    def _load_research(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_structured: bool,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Mapping[str, ResearchObservation]:
        if self._research_client is None:
            return {}
        now = self._monotonic()
        wall_now = self._wall_clock()
        result: dict[str, ResearchObservation] = {}
        previous: dict[str, _ResearchEntry] = {}
        with self._lock:
            for code in codes:
                entry = self._research.get((code, include_structured))
                if entry is None:
                    continue
                previous[code] = entry
                if not force and entry.expires_at > now:
                    result[code] = entry.observation
            for code in codes:
                if code in result:
                    continue
                cached = self._load_research_cache(code, include_structured, wall_now)
                if cached is not None:
                    self._research[(code, include_structured)] = cached
                    result[code] = cached.observation
                    previous[code] = cached
        missing = [code for code in codes if force or code not in result]
        if not missing:
            return result
        with borrow_executor(
            self._worker_pool,
            worker_count=min(self._research_workers, len(missing)),
            thread_name_prefix="candidate-research",
            queue_capacity=len(missing),
            wait_on_exit=deadline is None,
        ) as pool:
            futures = {}
            for code in missing:
                future = pool.submit(
                    self._fetch_research_observation,
                    code,
                    observed_at,
                    include_structured=include_structured,
                )
                if future is None:
                    raise RuntimeError("data worker queue rejected research task")
                futures[future] = code
            timeout = None if deadline is None else max(0.0, (deadline - self._wall_clock()).total_seconds())
            completed, pending = wait(futures, timeout=timeout)
            for future in completed:
                code = futures[future]
                ttl = self._research_ttl_seconds
                old_entry = previous.get(code)
                try:
                    observation = future.result()
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    observation = _degraded_research_observation(old_entry, str(exc))
                    ttl = min(60.0, ttl)
                    with self._lock:
                        self._research_error_count += 1
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
                        if observation.source_errors:
                            self._research_error_count += len(observation.source_errors)
                            self._research_last_error = observation.source_errors[-1][:240]
                            ttl = min(60.0, ttl)
                result[code] = observation
                self._write_research_cache(code, include_structured, observation, ttl, wall_now)
                with self._lock:
                    self._research[(code, include_structured)] = _ResearchEntry(
                        observation,
                        self._monotonic() + ttl,
                    )
            for future in pending:
                code = futures[future]
                future.cancel()
                observation = _degraded_research_observation(previous.get(code), "research_batch_deadline")
                result[code] = observation
                self._write_research_cache(code, include_structured, observation, ttl, wall_now)
                with self._lock:
                    self._research_error_count += 1
                    self._research_last_error = "research_batch_deadline"
                    self._research[(code, include_structured)] = _ResearchEntry(
                        observation,
                        self._monotonic() + min(60.0, self._research_ttl_seconds),
                    )
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
        if include_structured:
            return self._research_client.fetch_snapshot(code, observed_at=observed_at)
        return ResearchObservation(evidence=tuple(self._research_client.fetch_news(code, observed_at=observed_at)))
