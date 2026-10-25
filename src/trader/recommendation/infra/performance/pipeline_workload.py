"""Continuous offline replay from acquired quotes to paired in-memory publication.

This exercises production calculations, not the scheduler or supplier adapters.
Every tick intentionally requests a calculation, including unchanged inputs, so
cache behaviour is observable independently of the scheduler's unchanged gate.
"""

from __future__ import annotations

import cProfile
import math
import statistics
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import CodeType
from typing import TypeVar

from lightgbm import Booster

from trader.infra.cache import BoundedLruCache
from trader.infra.cache_contracts import CacheIdentity, CachePolicy, CacheStats
from trader.infra.serialization.canonical import ContentHash
from trader.infra.settings import StrategySettings
from trader.recommendation.application.pipeline.candidate_pool.candidate_builder import (
    BuildCandidatePlans,
    CandidatePlanningContext,
)
from trader.recommendation.application.pipeline.data_source.input_identity import InputVersionClock
from trader.recommendation.application.pipeline.final_selection.decision_projection import (
    BuildScoredLocal,
    ScoredLocalProjection,
    ScoredProjectionInputs,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.ports.loaded_profile import ModelComputationStatus
from trader.recommendation.application.ports.scoring import D25NativeInput, TomorrowNativeInput
from trader.recommendation.domain.market.models import FeatureSnapshot, MarketQuote
from trader.recommendation.domain.market.v1.features import LEARNED_RANK_FEATURE_IDS
from trader.recommendation.domain.publication.decision_identity import DecisionOverlay
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.normalization.features import FeatureBuilder
from trader.recommendation.infra.performance.fixtures import BuildPerformanceHistory
from trader.recommendation.infra.performance.model_workload import PerformanceProfileWorkload
from trader.recommendation.infra.performance.process_memory import PerformanceMemorySample

_T = TypeVar("_T")
_SCENARIOS = ("unchanged", "observation_time", "delivery_time", "quote_time", "one_price", "all_prices", "stale")
CONTINUOUS_REPLAY_CONTRACT = "acquired_quotes_to_paired_memory_publication"


@dataclass(frozen=True)
class PipelineReplayWorkload:
    model: PerformanceProfileWorkload
    strategy: StrategySettings


@dataclass
class _ReplayClock:
    current: datetime
    elapsed: float = 0.0

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        self.elapsed += seconds


class _Timings:
    def __init__(self) -> None:
        self.values: list[tuple[str, float]] = []

    def run(self, name: str, operation: Callable[[], _T]) -> _T:
        started = time.perf_counter()
        result = operation()
        self.values.append((name, (time.perf_counter() - started) * 1000))
        return result


class _ObservedFactorCache(BoundedLruCache[object]):
    """Count actual insertion attempts without changing the production cache path."""

    def __init__(self, policy: CachePolicy, clock: _ReplayClock) -> None:
        super().__init__(policy, monotonic=clock.monotonic, wall_clock=clock.now)
        self._counter_lock = threading.Lock()
        self._computations = 0
        self._codes: set[str] = set()

    def begin_round(self) -> None:
        with self._counter_lock:
            self._computations = 0
            self._codes.clear()

    def computations(self) -> tuple[int, int]:
        with self._counter_lock:
            return self._computations, len(self._codes)

    def put(
        self,
        identity: CacheIdentity,
        value: object,
        *,
        data_version: str,
        source_time: datetime,
        received_at: datetime | None = None,
    ) -> bool:
        if identity.dataset == "quote_factor_inputs":
            with self._counter_lock:
                self._computations += 1
                self._codes.add(identity.subject_key)
        return super().put(identity, value, data_version=data_version, source_time=source_time, received_at=received_at)


@dataclass(frozen=True)
class _HeadResult:
    strategy: Strategy
    input_count: int
    predicted_count: int
    selected_count: int
    predictor_batches: int
    model_cache_hits: int
    computed_groups: tuple[str, ...]
    reused_groups: tuple[str, ...]
    model_stages: tuple[tuple[str, float], ...]
    result_hash: str

    def project(self) -> dict[str, object]:
        return {
            "strategy": self.strategy.value,
            "input_count": self.input_count,
            "predicted_count": self.predicted_count,
            "selected_count": self.selected_count,
            "predictor_batches": self.predictor_batches,
            "model_cache_hits": self.model_cache_hits,
            "computed_groups": list(self.computed_groups),
            "reused_groups": list(self.reused_groups),
            "model_stage_ms": dict(self.model_stages),
            "result_hash": self.result_hash,
        }


@dataclass(frozen=True)
class _ProfileResult:
    profile: str
    wall_ms: float
    thread_cpu_ms: float
    input_revision_changed: bool
    input_version: str
    feature_hash: str
    stages: tuple[tuple[str, float], ...]
    heads: tuple[_HeadResult, ...]

    def project(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "wall_ms": self.wall_ms,
            "thread_cpu_ms": self.thread_cpu_ms,
            "input_revision_changed": self.input_revision_changed,
            "input_version": self.input_version,
            "feature_hash": self.feature_hash,
            "stage_ms": dict(self.stages),
            "heads": [head.project() for head in self.heads],
        }


@dataclass(frozen=True)
class _RunFacts:
    wall_ms: float
    cpu_ms: float
    changed: bool
    stages: tuple[tuple[str, float], ...]
    observations: tuple[tuple[Strategy, int, ModelComputationStatus, ModelComputationStatus], ...]


@dataclass(frozen=True)
class _TickResult:
    scenario: str
    round_number: int
    wall_ms: float
    cpu_ms: float
    observation_ms: float
    memory: PerformanceMemorySample
    cache_bytes: int
    cache_hits: int
    cache_misses: int
    factor_computations: int
    recomputed_stocks: int
    cache_evictions: int
    profiles: tuple[_ProfileResult, ...]

    def project(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "round": self.round_number,
            "wall_ms": self.wall_ms,
            "process_cpu_ms": self.cpu_ms,
            "observation_ms_excluded": self.observation_ms,
            "current_rss_bytes": self.memory.current_rss_bytes,
            "process_peak_rss_bytes": self.memory.peak_rss_bytes,
            "factor_cache_bytes": self.cache_bytes,
            "factor_cache_hits": self.cache_hits,
            "factor_cache_misses": self.cache_misses,
            "factor_computations": self.factor_computations,
            "recomputed_stocks": self.recomputed_stocks,
            "factor_cache_evictions": self.cache_evictions,
            "profiles": [profile.project() for profile in self.profiles],
        }


class _ProfileReplay:
    def __init__(self, workload: PipelineReplayWorkload, cache: BoundedLruCache[object], clock: _ReplayClock) -> None:
        self.workload = workload.model
        self.clock = clock
        self.scoring = workload.model.scoring()
        horizons = (20, 40, 60, 120, 250) if workload.model.profile.profile_id == "v2" else (20, 40, 60)
        self.builder = FeatureBuilder.from_settings(workload.strategy, horizons, cache)
        history = BuildPerformanceHistory(workload.model.features)
        self.histories = {item.code: item.bars for item in history}
        self.summaries = {item.code: item.context for item in history}
        self.completed = {item.code: item.model_feature for item in history}
        self.quotes = tuple(replace(item.quote, data_version="performance-replay") for item in workload.model.features)
        self.versions = InputVersionClock()
        self.version = ""
        self.index = UnifiedDecisionIndex()
        self.sequence = 0
        self.projections: tuple[ScoredLocalProjection, ...] = ()
        self.market: tuple[FeatureSnapshot, ...] = ()
        self.last_run: _RunFacts | None = None

    def change(self, scenario: str) -> None:
        now = self.clock.now()
        if scenario in {"delivery_time", "quote_time"}:
            self.quotes = tuple(
                replace(quote, received_time=now, source_time=now if scenario == "quote_time" else quote.source_time)
                for quote in self.quotes
            )
        elif scenario in {"one_price", "all_prices"}:
            count = 1 if scenario == "one_price" else len(self.quotes)
            self.quotes = tuple(
                replace(quote, price=quote.price + 0.001, received_time=now, source_time=now)
                if position < count and quote.price is not None
                else quote
                for position, quote in enumerate(self.quotes)
            )

    def _build(self, quotes: tuple[MarketQuote, ...], *, candidates: bool = False) -> tuple[FeatureSnapshot, ...]:
        features = self.builder.build(
            quotes,
            self.histories,
            self.clock.now(),
            history_summaries=self.summaries,
            cross_section_reference={item.quote.code: item.values for item in self.market} if candidates else None,
            cross_section_normalization_reference={item.quote.code: item.normalization for item in self.market}
            if candidates
            else None,
        )
        result: list[FeatureSnapshot] = []
        for feature in features:
            completed = self.completed[feature.quote.code]
            values = dict(feature.values)
            if self.workload.profile.profile_id == "v1":
                values.update((name, completed.values[name]) for name in LEARNED_RANK_FEATURE_IDS)
            result.append(
                replace(
                    feature,
                    values=values,
                    history_days=completed.history_days,
                    model_industry=completed.model_industry,
                    model_sequence=completed.model_sequence,
                    model_history_quality=completed.model_history_quality,
                )
            )
        return tuple(result)

    def _status(self, strategy: Strategy) -> ModelComputationStatus:
        status = self.scoring.status()
        if status is None:
            raise ValueError("continuous replay requires model computation counters")
        return status.heads[strategy].computation

    def run(self) -> None:
        self.sequence += 1
        timings = _Timings()
        start, cpu = time.perf_counter(), time.thread_time()
        self.market = timings.run("market_features", lambda: self._build(self.quotes))
        version = timings.run("input_identity", lambda: self.versions.features("market", self.market))
        changed = version != self.version
        self.version = version
        plans = timings.run(
            "candidate_filter",
            lambda: BuildCandidatePlans(
                self.market,
                None,
                CandidatePlanningContext(self.clock.now(), version, self.workload.policy, self.scoring),
            ),
        )
        union = frozenset(plans.physical_union())
        quotes = tuple(quote for quote in self.quotes if quote.code in union)
        # The production quote boundary enriches the refreshed union before preparing each head.
        timings.run("candidate_refresh_features", lambda: self._build(quotes, candidates=True))
        projections: list[ScoredLocalProjection] = []
        observations: list[tuple[Strategy, int, ModelComputationStatus, ModelComputationStatus]] = []
        for strategy, input_type in ((Strategy.TOMORROW, TomorrowNativeInput), (Strategy.D25, D25NativeInput)):
            requested = plans.strategy_codes(strategy)
            codes = frozenset(requested)
            candidates = timings.run(
                f"{strategy.value}.features",
                lambda: self._build(
                    tuple(quote for quote in quotes if quote.code in codes),
                    candidates=True,
                ),
            )
            native = input_type(
                self.clock.now().date(),
                "afternoon",
                version,
                self.workload.config_version,
                self.clock.now(),
                self.market,
                requested,
                candidates,
                30.0,
                30.0,
            )
            before = self._status(strategy)
            projection = timings.run(
                f"{strategy.value}.score_to_draft",
                lambda: BuildScoredLocal(
                    native,
                    self.workload.policy,
                    sequence=self.sequence,
                    runtime=ScoredProjectionInputs(model_scoring=self.scoring),
                ),
            )
            projections.append(projection)
            observations.append((strategy, len(candidates), before, self._status(strategy)))
        self.projections = tuple(projections)
        timings.run("paired_publish", self._publish)
        wall_ms, cpu_ms = (time.perf_counter() - start) * 1000, (time.thread_time() - cpu) * 1000
        self.last_run = _RunFacts(wall_ms, cpu_ms, changed, tuple(timings.values), tuple(observations))

    def observe(self) -> _ProfileResult:
        # Hashing is diagnostic work; collect it after all timed workers have finished.
        run = self.last_run
        if run is None:
            raise ValueError("continuous replay has not run")
        heads = tuple(
            _head_result(projection, *observation)
            for projection, observation in zip(self.projections, run.observations, strict=True)
        )
        return _ProfileResult(
            self.workload.profile.profile_id,
            run.wall_ms,
            run.cpu_ms,
            run.changed,
            self.version,
            ContentHash(tuple((item.quote.code, item.values, item.model_history_quality) for item in self.market)),
            run.stages,
            heads,
        )

    def _publish(self) -> None:
        first, second = (projection.local for projection in self.projections)
        overlays = tuple(
            DecisionOverlay(
                item.strategy,
                item.trade_date,
                item.version,
                self.clock.now(),
                tuple(row.quote for row in item.items if row.quote is not None),
                sequence=self.sequence,
            )
            for item in (first, second)
        )
        previous = tuple(self.index.snapshot(item.strategy).current for item in (first, second))
        result = self.index.publish_scored_pair(
            (first, second),
            (overlays[0], overlays[1]),
            (
                previous[0].version if previous[0] is not None else None,
                previous[1].version if previous[1] is not None else None,
            ),
        )
        if not result.accepted:
            raise ValueError(f"continuous replay publication rejected: {result.reason}")


def _head_result(
    projection: ScoredLocalProjection,
    strategy: Strategy,
    input_count: int,
    before: ModelComputationStatus,
    after: ModelComputationStatus,
) -> _HeadResult:
    previous = {item.calculator_group: item.cumulative_duration_ms for item in before.stage_durations}
    business = (
        projection.model_diagnostics,
        tuple(
            (
                item.code,
                item.disposition,
                item.filter_reasons,
                item.optional_flags,
                item.local_components,
                item.local_base_score,
                item.local_risk_penalty,
                item.local_score,
                item.board_rank,
                item.rank,
                item.selection_skip_reason,
                item.execution_gate_reason,
            )
            for item in projection.selection.evaluations
        ),
        tuple((item.code, item.final_score, item.action, item.rank) for item in projection.local.items),
    )
    return _HeadResult(
        strategy,
        input_count,
        len(projection.model_diagnostics),
        len(projection.local.items),
        after.predictor_batch_count - before.predictor_batch_count,
        after.cache_hit_count - before.cache_hit_count,
        after.computed_groups if after.request_count > before.request_count else (),
        after.reused_groups if after.request_count > before.request_count else (),
        tuple(
            (item.calculator_group, item.cumulative_duration_ms - previous.get(item.calculator_group, 0.0))
            for item in after.stage_durations
        ),
        ContentHash(business),
    )


def _cache_stats(cache: BoundedLruCache[object]) -> tuple[CacheStats, ...]:
    return tuple(cache.status().datasets.get("quote_factor_inputs", {}).values())


def _distribution(values: tuple[float, ...]) -> dict[str, object]:
    ordered = sorted(values)
    return {
        "samples": len(values),
        "p50_ms": statistics.median(ordered),
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "maximum_ms": ordered[-1],
    }


def MeasureContinuousPipeline(
    workloads: tuple[PipelineReplayWorkload, ...],
    cache_policy: CachePolicy,
    executor: Executor,
    read_memory: Callable[[], PerformanceMemorySample],
    *,
    ticks: int,
) -> dict[str, object]:
    """Keep one clock, cache, model service and publication index throughout all cases."""
    if not workloads or not 1 <= ticks <= 1000:
        raise ValueError("continuous replay requires profiles and 1..1000 ticks per scenario")
    clock = _ReplayClock(workloads[0].model.features[0].observed_at)
    cache = _ObservedFactorCache(cache_policy, clock)
    preparation = time.perf_counter()
    try:
        states = tuple(_ProfileReplay(workload, cache, clock) for workload in workloads)
        preparation_ms = (time.perf_counter() - preparation) * 1000
        rows: list[_TickResult] = []
        for scenario in ("cold", *_SCENARIOS):
            for tick in range(1 if scenario == "cold" else ticks):
                if scenario != "unchanged" and scenario != "cold":
                    clock.advance(31.0 if scenario == "stale" and tick == 0 else 2.0 / ticks)
                for state in states:
                    state.change(scenario)
                before = _cache_stats(cache)
                cache.begin_round()
                started, cpu = time.perf_counter(), time.process_time()
                tuple(executor.map(lambda state: state.run(), states))
                wall_ms = (time.perf_counter() - started) * 1000
                cpu_ms = (time.process_time() - cpu) * 1000
                observation_started = time.perf_counter()
                results = tuple(state.observe() for state in states)
                after = _cache_stats(cache)
                observation_ms = (time.perf_counter() - observation_started) * 1000
                rows.append(
                    _TickResult(
                        scenario,
                        tick + 1,
                        wall_ms,
                        cpu_ms,
                        observation_ms,
                        read_memory(),
                        sum(item.estimated_bytes for item in after),
                        sum(item.hit for item in after) - sum(item.hit for item in before),
                        sum(item.miss for item in after) - sum(item.miss for item in before),
                        *cache.computations(),
                        sum(item.eviction for item in after) - sum(item.eviction for item in before),
                        results,
                    )
                )
        # Profiling is a separate additional unchanged request on the already warm services.
        probes = tuple(executor.map(_profile, states))
        return _project(rows, preparation_ms, probes, cache_policy, ticks)
    finally:
        cache.stop()


def _profile(state: _ProfileReplay) -> tuple[str, tuple[tuple[str, int], ...]]:
    profiler = cProfile.Profile()
    profiler.runcall(state.run)
    owners = (
        ("base_factor_computations", "/normalization/features.py", "_base_features"),
        ("raw_factor_requests", "/normalization/features.py", "_raw_features"),
        ("ridge_batch_evaluations", "/model_bundles/predictor.py", "_predict_ridge_batch"),
        ("tcn_predict_calls", "/profile/v5/network.py", "predict"),
    )
    counts = tuple(
        (
            name,
            sum(
                entry.callcount
                for entry in profiler.getstats()
                if isinstance(entry.code, CodeType)
                and entry.code.co_filename.replace("\\", "/").endswith(filename)
                and entry.code.co_name == function
            ),
        )
        for name, filename, function in owners
    )
    native_predict = sum(entry.callcount for entry in profiler.getstats() if entry.code is Booster.predict.__code__)
    return state.workload.profile.profile_id, (*counts, ("lightgbm_predict_calls", native_predict))


def _project(
    rows: list[_TickResult],
    preparation_ms: float,
    probes: tuple[tuple[str, tuple[tuple[str, int], ...]], ...],
    cache_policy: CachePolicy,
    ticks: int,
) -> dict[str, object]:
    before = rows[0].memory.current_rss_bytes
    maximum = max(row.memory.current_rss_bytes for row in rows)
    return {
        "contract": CONTINUOUS_REPLAY_CONTRACT,
        "ticks_per_scenario": ticks,
        "execution_mode": "concurrent_profiles_serial_heads_shared_factor_cache",
        "scope": "synthetic_acquired_quotes_production_calculations_paired_memory_publish",
        "exclusions": [
            "supplier_io",
            "scheduler_gating",
            "industry_refresh_cadence",
            "minute_inputs",
            "research_inputs",
            "disk_preload",
            "formal_freeze",
            "web_delivery",
        ],
        "preparation_ms_excluded": preparation_ms,
        "timing": "profile stages are disjoint; model stages are nested in score_to_draft, never add them",
        "cpu_scope": "process CPU excludes observation hashes; per-profile thread CPU excludes native worker CPU",
        "cache_counting": "computations count completed factor evaluations before insertion, including rejected writes; hits alone do not prove reuse",
        "invalidation": "scenario is the controlled input change; input_revision_changed and model groups are observed, not inferred causes",
        "cold": rows[0].project(),
        "scenarios": {
            name: {
                **_distribution(tuple(row.wall_ms for row in rows if row.scenario == name)),
                "rounds": [row.project() for row in rows if row.scenario == name],
            }
            for name in _SCENARIOS
        },
        "memory": {
            "rss_after_cold_bytes": before,
            "maximum_current_rss_bytes": maximum,
            "rss_after_bytes": rows[-1].memory.current_rss_bytes,
            "growth_percent": max(0.0, (maximum - before) / before * 100),
            "peak_rss_bytes": max(row.memory.peak_rss_bytes for row in rows),
            "factor_cache_max_bytes": max(row.cache_bytes for row in rows),
            "factor_group_budget_bytes": cache_policy.groups[
                cache_policy.datasets["quote_factor_inputs"].group
            ].max_bytes,
        },
        "cpu_probe": {
            "scenario": "stale_exact_repeat_after_latency_samples",
            "profiles": {profile: dict(counts) for profile, counts in probes},
        },
    }


__all__ = ["CONTINUOUS_REPLAY_CONTRACT", "PipelineReplayWorkload", "MeasureContinuousPipeline"]
