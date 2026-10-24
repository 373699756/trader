"""Offline measurements of real scoring capabilities over immutable fixture inputs."""

from __future__ import annotations

import cProfile
import gc
import math
import statistics
import time
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass, replace
from types import CodeType

from lightgbm import Booster

from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.application.pipeline.final_selection.decision_projection import (
    BuildScoredLocal,
    ScoredLocalProjection,
    ScoredProjectionInputs,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.ports.loaded_profile import (
    LoadedScoringProfile,
    ModelComputationStatus,
    ModelScoreBatch,
    ModelScoringContext,
    ModelScoringPort,
)
from trader.recommendation.application.ports.scoring import D25NativeInput, ScoredNativeInput, TomorrowNativeInput
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.recommendation.infra.performance.process_memory import PerformanceMemorySample
from trader.recommendation.infra.scoring.profile_factory import BuildProfileScoring


@dataclass(frozen=True)
class PerformanceProfileWorkload:
    profile: LoadedScoringProfile
    policy: RecommendationPolicy
    features: tuple[FeatureSnapshot, ...]
    config_version: str
    cost_policy: ExecutionCostPolicy

    def scoring(self) -> ModelScoringPort:
        return BuildProfileScoring(
            self.profile.profile_id,
            self.profile,
            history_sessions=max(feature.history_days for feature in self.features),
            model_failure="",
            cost_policy=self.cost_policy,
            maintenance=None,
        )


@dataclass(frozen=True)
class _HeadWorkload:
    profile_id: ScoringProfileId
    scoring: ModelScoringPort
    native: ScoredNativeInput
    policy: RecommendationPolicy


@dataclass(frozen=True)
class PerformanceHeadMeasurement:
    profile_id: ScoringProfileId
    strategy: Strategy
    input_count: int
    predicted_count: int
    positive_utility_count: int
    predictor_calls: int
    cache_hits: int
    result_hash: str
    stages: tuple[tuple[str, float], ...]
    native_calls: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class PerformanceScenarioMeasurement:
    scenario: str
    durations_ms: tuple[float, ...]
    heads: tuple[PerformanceHeadMeasurement, ...]
    preparation_ms: tuple[float, ...] = ()
    priming_ms: tuple[float, ...] = ()

    def project(self) -> dict[str, object]:
        ordered = sorted(self.durations_ms)
        return {
            "p50_ms": statistics.median(ordered),
            "p95_ms": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)],
            "maximum_ms": max(ordered),
            "samples": len(ordered),
            "input_preparation_ms": list(self.preparation_ms),
            "cache_priming_ms": list(self.priming_ms),
            "heads": [
                {
                    "profile": head.profile_id,
                    "strategy": head.strategy.value,
                    "input_count": head.input_count,
                    "predicted_count": head.predicted_count,
                    "positive_utility_count": head.positive_utility_count,
                    "predictor_calls": head.predictor_calls,
                    "cache_hits": head.cache_hits,
                    "result_hash": head.result_hash,
                    "stage_duration_ms": dict(head.stages),
                    "native_calls": dict(head.native_calls),
                }
                for head in self.heads
            ],
        }


@dataclass(frozen=True)
class PerformanceSteadyStateMeasurement:
    ticks: int
    rss_before_bytes: int
    rss_after_bytes: int
    peak_rss_bytes: int
    maximum_current_rss_bytes: int

    @property
    def growth_percent(self) -> float:
        return max(0.0, (self.maximum_current_rss_bytes - self.rss_before_bytes) / self.rss_before_bytes * 100.0)


@dataclass(frozen=True)
class _HeadExecution:
    head: _HeadWorkload
    before: ModelComputationStatus
    after: ModelComputationStatus
    batch: ModelScoreBatch | None = None
    projection: ScoredLocalProjection | None = None


def MeasureModelWorkload(
    workloads: tuple[PerformanceProfileWorkload, ...],
    executor: Executor,
    *,
    warmup: int,
    rounds: int,
) -> tuple[PerformanceScenarioMeasurement, ...]:
    """Measure one process and concurrent heads, never sum independent percentiles."""
    if not workloads or rounds < 1 or warmup < 0:
        raise ValueError("performance workload and round counts are required")
    results: list[PerformanceScenarioMeasurement] = []
    for scenario in ("first_computation", "exact_reuse", "single_stock_change", "quote_to_draft"):
        durations: list[float] = []
        preparations: list[float] = []
        primings: list[float] = []
        observations: tuple[PerformanceHeadMeasurement, ...] = ()
        for iteration in range(warmup + rounds):
            prepare_started = time.perf_counter()
            heads = _prepare_heads(workloads, changed=scenario == "single_stock_change")
            prepared_ms = (time.perf_counter() - prepare_started) * 1000.0
            priming_ms = 0.0
            if scenario in {"exact_reuse", "single_stock_change"}:
                original = _prepare_original(heads, workloads)
                prime_started = time.perf_counter()
                tuple(executor.map(_score_head, original))
                priming_ms = (time.perf_counter() - prime_started) * 1000.0
            started = time.perf_counter()
            action = _project_head if scenario == "quote_to_draft" else _score_head
            executions = tuple(executor.map(action, heads))
            elapsed = (time.perf_counter() - started) * 1000.0
            observations = tuple(_observation(item) for item in executions)
            if iteration >= warmup:
                durations.append(elapsed)
                preparations.append(prepared_ms)
                primings.append(priming_ms)
        # Deterministic profiler counters are collected separately, outside latency samples.
        if scenario != "quote_to_draft":
            heads = _prepare_heads(workloads, changed=scenario == "single_stock_change")
            if scenario in {"exact_reuse", "single_stock_change"}:
                tuple(executor.map(_score_head, _prepare_original(heads, workloads)))
            profiled = tuple(executor.map(_profile_head, heads))
            if tuple(item.result_hash for item in profiled) != tuple(item.result_hash for item in observations):
                raise ValueError("performance profiling changed model outputs")
            observations = profiled
        results.append(
            PerformanceScenarioMeasurement(
                scenario, tuple(durations), observations, tuple(preparations), tuple(primings)
            )
        )
    return tuple(results)


def MeasureSteadyState(
    workloads: tuple[PerformanceProfileWorkload, ...],
    executor: Executor,
    read_memory: Callable[[], PerformanceMemorySample],
    *,
    ticks: int = 100,
) -> PerformanceSteadyStateMeasurement:
    """Keep the same scoring caches alive for every tick; sample actual resident memory."""
    if ticks < 1:
        raise ValueError("performance steady-state tick count must be positive")
    heads = _prepare_heads(workloads, changed=False)
    tuple(executor.map(_score_head, heads))
    gc.collect()
    before = read_memory()
    maximum = before.current_rss_bytes
    peak = before.peak_rss_bytes
    current = before
    for _ in range(ticks):
        tuple(executor.map(_score_head, heads))
        current = read_memory()
        maximum = max(maximum, current.current_rss_bytes)
        peak = max(peak, current.peak_rss_bytes)
    return PerformanceSteadyStateMeasurement(ticks, before.current_rss_bytes, current.current_rss_bytes, peak, maximum)


def _prepare_heads(workloads: tuple[PerformanceProfileWorkload, ...], *, changed: bool) -> tuple[_HeadWorkload, ...]:
    heads: list[_HeadWorkload] = []
    for workload in workloads:
        features = _changed_features(workload.features) if changed else workload.features
        scoring = workload.scoring()
        for strategy, input_type in ((Strategy.TOMORROW, TomorrowNativeInput), (Strategy.D25, D25NativeInput)):
            if not scoring.uses_model(strategy) or not all(
                scoring.is_input_eligible(strategy, item) for item in features
            ):
                raise ValueError("performance fixture is not eligible for its complete model head")
            native = input_type(
                features[0].observed_at.date(),
                "afternoon",
                "performance-single-change" if changed else "performance-complete",
                workload.config_version,
                features[0].observed_at,
                features,
                tuple(item.quote.code for item in features),
                features,
                30.0,
                30.0,
            )
            heads.append(_HeadWorkload(workload.profile.profile_id, scoring, native, workload.policy))
    return tuple(heads)


def _prepare_original(
    heads: tuple[_HeadWorkload, ...], workloads: tuple[PerformanceProfileWorkload, ...]
) -> tuple[_HeadWorkload, ...]:
    originals = {item.profile.profile_id: item.features for item in workloads}
    return tuple(
        replace(
            head,
            native=replace(
                head.native,
                data_version="performance-complete",
                market_features=originals[head.profile_id],
                candidate_features=originals[head.profile_id],
            ),
        )
        for head in heads
    )


def _changed_features(features: tuple[FeatureSnapshot, ...]) -> tuple[FeatureSnapshot, ...]:
    # One changed raw return invalidates only the dependency stages it actually feeds.
    first = features[0]
    values = dict(first.values)
    values["qfq_return_3d"] = float(values["qfq_return_3d"] or 0.0) + 0.0001
    if "completed_return_3d" in values:
        values["completed_return_3d"] = float(values["completed_return_3d"] or 0.0) + 0.0001
    return (replace(first, values=values), *features[1:])


def _status(head: _HeadWorkload) -> ModelComputationStatus:
    status = head.scoring.status()
    if status is None or head.native.strategy not in status.heads:
        raise ValueError("performance head status is unavailable")
    return status.heads[head.native.strategy].computation


def _score_head(head: _HeadWorkload) -> _HeadExecution:
    before = _status(head)
    batch = head.scoring.score(
        head.native.strategy,
        head.native.candidate_features,
        context=ModelScoringContext(
            population_features=head.native.market_features,
            population_identity="performance-fixed-population",
        ),
    )
    if batch is None or batch.missing_codes:
        raise ValueError("performance model did not predict its complete fixture population")
    if tuple(item.code for item in batch.predictions) != tuple(sorted(head.native.requested_codes)):
        raise ValueError("performance prediction population does not match fixture")
    return _HeadExecution(head, before, _status(head), batch=batch)


def _project_head(head: _HeadWorkload) -> _HeadExecution:
    before = _status(head)
    projection = BuildScoredLocal(
        head.native,
        head.policy,
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=head.scoring),
    )
    return _HeadExecution(head, before, _status(head), projection=projection)


def _observation(execution: _HeadExecution) -> PerformanceHeadMeasurement:
    head, before, after = execution.head, execution.before, execution.after
    if execution.batch is not None:
        predicted = len(execution.batch.predictions)
        positive = sum(value.predicted_net_excess_pct > 0 for value in execution.batch.diagnostics.values())
        result_hash = ContentHash(execution.batch.predictions)
    elif execution.projection is not None:
        predicted = len(execution.projection.model_diagnostics)
        positive = sum(value.predicted_net_excess_pct > 0 for _code, value in execution.projection.model_diagnostics)
        result_hash = ContentHash(
            tuple((item.code, item.final_score, item.action.value) for item in execution.projection.local.items)
        )
    else:
        raise ValueError("performance execution has no result")
    return PerformanceHeadMeasurement(
        head.profile_id,
        head.native.strategy,
        len(head.native.candidate_features),
        predicted,
        positive,
        after.predictor_batch_count - before.predictor_batch_count,
        after.cache_hit_count - before.cache_hit_count,
        result_hash,
        _stage_deltas(before, after),
    )


def _stage_deltas(before: ModelComputationStatus, after: ModelComputationStatus) -> tuple[tuple[str, float], ...]:
    previous = {item.calculator_group: item.cumulative_duration_ms for item in before.stage_durations}
    return tuple(
        (item.calculator_group, max(0.0, item.cumulative_duration_ms - previous.get(item.calculator_group, 0.0)))
        for item in after.stage_durations
    )


def _profile_head(head: _HeadWorkload) -> PerformanceHeadMeasurement:
    profiler = cProfile.Profile()
    measured = _observation(profiler.runcall(_score_head, head))
    counts = {
        "lightgbm_predict_calls": 0,
        "tcn_predict_calls": 0,
        "ridge_batch_evaluations": 0,
        "model_row_builds": 0,
        "cost_reference_builds": 0,
    }
    for entry in profiler.getstats():
        code = entry.code
        if not isinstance(code, CodeType):
            continue
        filename = code.co_filename.replace("\\", "/")
        if code is Booster.predict.__code__:
            counts["lightgbm_predict_calls"] += entry.callcount
        elif filename.endswith("/profile/v5/network.py") and code.co_name == "predict":
            counts["tcn_predict_calls"] += entry.callcount
        elif filename.endswith("/model_bundles/predictor.py") and code.co_name == "_predict_ridge_batch":
            counts["ridge_batch_evaluations"] += entry.callcount
        elif filename.endswith("/local_score/model_scoring.py") and code.co_name == "_eligible_rows":
            counts["model_row_builds"] += entry.callcount
        elif filename.endswith("/local_score/model_scoring.py") and code.co_name == "_build_cost_reference":
            counts["cost_reference_builds"] += entry.callcount
    return replace(measured, native_calls=tuple(counts.items()))


__all__ = [
    "PerformanceProfileWorkload",
    "PerformanceHeadMeasurement",
    "PerformanceScenarioMeasurement",
    "MeasureModelWorkload",
    "MeasureSteadyState",
    "PerformanceSteadyStateMeasurement",
]
