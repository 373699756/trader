"""Deterministic offline performance gate over active production functions."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import statistics
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import cast

from trader.bootstrap import BuildPerformanceExecutor
from trader.http_api.route_services import UnifiedWebServices
from trader.infra.serialization.canonical import ContentHash
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.settings import LoadRuntimeSettings, LoadStrategySettings, RuntimeSettings
from trader.infra.settings.models import PerformanceBudgetSettings
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.workers import WorkerFutureExecutor
from trader.recommendation.application.pipeline.candidate_pool.candidate_builder import (
    BuildCandidatePlans,
    CandidatePlanningContext,
    CandidatePlanSet,
)
from trader.recommendation.application.pipeline.final_selection.decision_projection import (
    BuildScoredHybrid,
    BuildScoredLocal,
    ScoredLocalProjection,
    ScoredProjectionInputs,
)
from trader.recommendation.application.pipeline.freeze_publish.decision_events import BuildDecisionCommitted
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedDecisionEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedDecisionQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.ports.loaded_profile import ModelScoringPort
from trader.recommendation.application.ports.scoring import TomorrowNativeInput
from trader.recommendation.domain.candidate.composition import LocalScoreResult
from trader.recommendation.domain.evidence.review import DeepSeekReview, ReviewOutcome
from trader.recommendation.domain.market.models import Board, FeatureSnapshot, MarketQuote
from trader.recommendation.domain.publication.decision_identity import (
    CommittedDecisionRecord,
    DecisionItem,
    DecisionOverlay,
    DecisionQuote,
    ScoredDecision,
)
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection
from trader.recommendation.domain.scoring.scoring import ScoreBoardStrategy
from trader.recommendation.infra.normalization.columnar import ColumnarQuoteBatch, TargetedMarketChanges
from trader.recommendation.infra.normalization.merge import (
    MergeMarketObservations,
    MergeObservationLimits,
    ObservationFromQuote,
    OverlayCanonicalSnapshot,
)
from trader.recommendation.infra.normalization.quote import BuildMarketQuote, MarketQuoteInput
from trader.recommendation.infra.performance.cpu_trace import PerformanceCpuTrace
from trader.recommendation.infra.performance.fixtures import (
    BuildPerformanceFixtures,
    BuildWorstCaseCandidatePlans,
    CompleteRealtimeObservations,
    ProjectCandidateUnionQuotes,
)
from trader.recommendation.infra.performance.model_workload import (
    MeasureModelWorkload,
    MeasureSteadyState,
    PerformanceProfileWorkload,
)
from trader.recommendation.infra.performance.pipeline_workload import (
    CONTINUOUS_REPLAY_CONTRACT,
    MeasureContinuousPipeline,
    PipelineReplayWorkload,
)
from trader.recommendation.infra.performance.process_memory import ReadPerformanceMemory
from trader.recommendation.infra.policy_settings import _recommendation_policy
from trader.recommendation.infra.scoring.cost_settings import LoadExecutionCostPolicy
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile, ScoringModelsUnavailableError
from trader.web import CreateApp


@dataclass(frozen=True)
class _OperationContext:
    config_version: str
    policy: RecommendationPolicy
    model_scoring: ModelScoringPort


@dataclass(frozen=True)
class _CandidateUnionFixture:
    plans: CandidatePlanSet
    quotes: tuple[MarketQuote, ...]


def BuildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--profile", choices=("v1", "v2", "v3", "v5", "v1,v2,v3,v5"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--cpu-profile-dir", type=Path, help="instrumented replay evidence; not a latency gate")
    parser.add_argument("--cpu-profile-mode", choices=("functions", "gc"), default="functions")
    parser.add_argument(
        "--replay-rows", type=int, help="bounded replay population; cannot establish the full-market gate"
    )
    parser.add_argument(
        "--replay-ticks",
        type=int,
        default=0,
        help="1..1000 ticks per continuous scenario; 0 keeps model microbenchmarks",
    )
    return parser


def Run(
    config_path: Path,
    *,
    baseline_path: Path | None = None,
    scoring_profile: str | None = None,
    replay_ticks: int = 0,
    replay_rows: int | None = None,
    trace: PerformanceCpuTrace | None = None,
) -> dict[str, object]:
    if not 0 <= replay_ticks <= 1000:
        raise ValueError("replay ticks must be within 0..1000")
    if replay_rows is not None and (replay_rows < 1 or not replay_ticks):
        raise ValueError("bounded replay rows require continuous replay and a positive population")
    if trace is not None and (replay_ticks != 1 or baseline_path is not None):
        raise ValueError("CPU tracing requires one tick per scenario and no latency baseline")
    settings = LoadRuntimeSettings(config_path)
    selected = (
        ParseProfileSelection(scoring_profile)
        if scoring_profile
        else (LoadStrategySettings(settings.strategy_config_path).scoring_profile,)
    )
    if len(selected) not in {1, 4}:
        raise ValueError("performance accepts one profile or the complete four-profile selection")

    def prepare() -> _PerformancePreparation:
        return _prepare_profiles(settings, selected, config_path, replay_rows=replay_rows)

    prepared = prepare() if trace is None else trace.run("prepare_profiles", prepare)
    budgets = settings.performance_budgets
    workloads = prepared.workloads
    market_inputs, market_quotes = prepared.market_inputs, prepared.market_quotes
    profile_results = _project_profile_loads(prepared.profiles)
    if len(workloads) != len(selected):
        report = _unavailable_report("model_unavailable")
        report.update(
            profile_results=profile_results,
            workload={"profiles": list(selected), "profile_count": len(selected), "model_heads": 2 * len(selected)},
        )
        return report
    first = workloads[0]
    candidates = first.features
    identity: dict[str, object] = {
        "config_version": settings.config_version,
        "scoring_profile": ",".join(selected),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "fixture_sha256": ContentHash(tuple((item.profile.profile_id, item.features) for item in workloads)),
        "models_sha256": ContentHash(
            tuple(
                (
                    item.profile.profile_id,
                    tuple((head.value, runtime.predictor.model_hash) for head, runtime in item.profile.heads.items()),
                )
                for item in workloads
            )
        ),
        "strategy_contracts_sha256": CanonicalDigest(tuple(item.policy for item in workloads)),
        "cost_policy_sha256": ContentHash(workloads[0].cost_policy),
        "performance_budgets_sha256": ContentHash(budgets),
        "dependencies": {
            name: version(name) for name in ("numpy", "lightgbm", *(("torch",) if "v5" in selected else ()))
        },
        "source_sha256": _source_digest(Path(__file__).resolve().parents[2]),
        "python": platform.python_version(),
        "system": platform.system(),
        "cpu_count": os.cpu_count(),
        "cpu": _cpu_identity(),
        "machine": platform.machine(),
        "kernel": platform.release(),
        "replay_ticks": replay_ticks,
        "replay_rows": replay_rows,
        "continuous_contract": CONTINUOUS_REPLAY_CONTRACT if replay_ticks else None,
        "instrumented": trace is not None,
    }
    baseline = _baseline(baseline_path, identity)
    if replay_ticks:
        return _continuous_report(settings, prepared, identity, replay_ticks, baseline, trace)
    context = _OperationContext(settings.config_version, first.policy, first.scoring())
    worst_case_plans = BuildWorstCaseCandidatePlans(budgets.workload.market_rows, market_quotes[0].received_time)
    union = ProjectCandidateUnionQuotes(market_quotes, worst_case_plans)
    operations, provenance = _operations(
        market_inputs, market_quotes, candidates, _CandidateUnionFixture(worst_case_plans, union), context
    )
    measurements = {
        name: _measure(operation, budgets.rounds.warmup, budgets.rounds.measurement)
        for name, operation in operations.items()
    }
    workers = BuildPerformanceExecutor(2 * len(workloads))
    workers.start()
    try:
        executor = WorkerFutureExecutor(workers)
        scenarios = MeasureModelWorkload(
            tuple(workloads), executor, warmup=budgets.rounds.warmup, rounds=budgets.rounds.measurement
        )
        steady = MeasureSteadyState(tuple(workloads), executor, ReadPerformanceMemory)
    finally:
        stopped = workers.stop(wait=True, cancel_futures=True, deadline=ShutdownDeadline.start(30.0))
        if not stopped.completed:
            raise RuntimeError("performance workers did not stop within the shutdown budget")
    model_workload = {item.scenario: item.project() for item in scenarios}
    # The gate uses actual simultaneous cold pipelines, rather than a sum of isolated P95s.
    measurements["quote_to_draft"] = {key: value for key, value in scenarios[-1].project().items() if key != "heads"}
    provenance["quote_to_draft"] = (
        "BuildScoredLocal: simultaneous Tomorrow/D25 for every selected profile; cold scoring state"
    )
    failures = _failures(measurements, budgets, baseline)
    for scenario in (scenarios[0], scenarios[-1]):
        if any(head.predicted_count != head.input_count or head.predictor_calls == 0 for head in scenario.heads):
            failures.append(f"{scenario.scenario}:model_population_not_exercised")
    growth = steady.growth_percent
    if steady.peak_rss_bytes > budgets.memory.process_peak_rss_bytes:
        failures.append("process_peak_rss:absolute_budget")
    if growth > budgets.memory.growth_percent:
        failures.append("process_rss_growth:absolute_budget")
    return {
        "schema_version": "production_performance",
        "status": "passed" if not failures else "failed",
        "identity": identity,
        "workload": {
            "profiles": list(selected),
            "profile_count": len(selected),
            "model_heads": 2 * len(selected),
            "execution_mode": "concurrent_heads_same_process",
            "evidence_scope": "generated_fixture_real_loaded_models_no_release_authority",
            "market_rows": len(market_inputs),
            "candidate_rows": len(union),
            "candidate_rows_per_strategy": len(candidates),
            "strategies": 2,
            "warmup_rounds": budgets.rounds.warmup,
            "measurement_rounds": budgets.rounds.measurement,
            "fixture_build_seconds": prepared.fixture_seconds,
            "history_scan_count": 0,
            "history_preload_status": "not_measured_generated_fixture",
            "tick_count": steady.ticks,
        },
        "profile_results": profile_results,
        "measurements": measurements,
        "model_workload": model_workload,
        "model_call_counting": {
            "method": "separate_cprofile_probe_excluded_from_latency",
            "lightgbm_predict_calls": "lightgbm.Booster.predict only; excludes nested wrapper",
            "tcn_predict_calls": "TcnNetwork.predict",
            "ridge_batch_evaluations": "bounded Ridge batches preserving scalar dot reduction; not a BLAS kernel count",
        },
        "operation_provenance": provenance,
        "memory": {
            "rss_before_kib": steady.rss_before_bytes / 1024,
            "rss_after_kib": steady.rss_after_bytes / 1024,
            "peak_rss_bytes": steady.peak_rss_bytes,
            "maximum_current_rss_bytes": steady.maximum_current_rss_bytes,
            "growth_percent": growth,
            "peak_budget_bytes": budgets.memory.process_peak_rss_bytes,
            "sampling": "current_rss_each_tick_and_process_peak",
            "logical_cache_status": "not_measured_fixture_has_no_market_cache",
        },
        "relative_regression_percent": budgets.relative_regression_percent,
        "network_calls": 0,
        "failures": failures,
        "hot_path_baseline": {
            "schema_version": "scoring_hot_path_efficiency_baseline",
            "status": "unverified",
            "reason": "formal_history_and_equivalence_cases_not_measured",
        },
    }


@dataclass(frozen=True)
class _ProfileLoad:
    profile_id: ScoringProfileId
    reason: str | None
    input_count: int = 0
    models: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _PerformancePreparation:
    workloads: tuple[PerformanceProfileWorkload, ...]
    profiles: tuple[_ProfileLoad, ...]
    market_inputs: tuple[MarketQuoteInput, ...]
    market_quotes: tuple[MarketQuote, ...]
    fixture_seconds: float


def _continuous_report(
    settings: RuntimeSettings,
    prepared: _PerformancePreparation,
    identity: dict[str, object],
    ticks: int,
    baseline: dict[str, object],
    trace: PerformanceCpuTrace | None = None,
) -> dict[str, object]:
    workloads = tuple(
        PipelineReplayWorkload(
            item,
            LoadStrategySettings(settings.strategy_config_path, scoring_profile=item.profile.profile_id),
        )
        for item in prepared.workloads
    )
    workers = BuildPerformanceExecutor(len(workloads))
    workers.start()
    try:
        replay = MeasureContinuousPipeline(
            workloads,
            settings.market_data.cache_policy,
            WorkerFutureExecutor(workers),
            ReadPerformanceMemory,
            ticks=ticks,
            trace=trace,
        )
    finally:
        stopped = workers.stop(wait=True, cancel_futures=True, deadline=ShutdownDeadline.start(30.0))
        if not stopped.completed:
            raise RuntimeError("continuous replay workers did not stop")
    memory = cast(dict[str, float], replay["memory"])
    scenarios = cast(dict[str, dict[str, object]], replay["scenarios"])
    measurements = {
        f"continuous_{name}": {key: case[key] for key in ("samples", "p50_ms", "p95_ms", "maximum_ms")}
        for name, case in scenarios.items()
    }
    failures: list[str] = []
    limits = settings.performance_budgets.memory
    cold = cast(dict[str, object], replay["cold"])
    for profile in cast(list[dict[str, object]], cold["profiles"]):
        for head in cast(list[dict[str, object]], profile["heads"]):
            if head["predicted_count"] == 0 or head["predictor_batches"] == 0:
                failures.append(f"continuous_cold:{profile['profile']}:{head['strategy']}:model_not_exercised")
    if memory["peak_rss_bytes"] > limits.process_peak_rss_bytes:
        failures.append("process_peak_rss:absolute_budget")
    if memory["growth_percent"] > limits.growth_percent:
        failures.append("process_rss_growth:absolute_budget")
    if memory["factor_cache_max_bytes"] > memory["factor_group_budget_bytes"]:
        failures.append("factor_cache:absolute_budget")
    for name, measurement in measurements.items():
        previous = baseline.get(name)
        if isinstance(previous, dict) and isinstance(previous.get("p95_ms"), (int, float)):
            if cast(float, measurement["p95_ms"]) > previous["p95_ms"] * (
                1 + settings.performance_budgets.relative_regression_percent / 100
            ):
                failures.append(f"{name}:relative_regression")
    return {
        "schema_version": "production_performance",
        "status": "failed" if failures else "passed",
        "identity": {**identity, "continuous_contract": replay["contract"]},
        "profile_results": _project_profile_loads(prepared.profiles),
        "workload": {
            "profiles": [item.model.profile.profile_id for item in workloads],
            "market_rows": len(prepared.market_quotes),
            "configured_market_rows": settings.performance_budgets.workload.market_rows,
            "acceptance": "diagnostic_execution_only_not_full_market_acceptance",
            "ticks_per_scenario": ticks,
            "fixture_build_seconds": prepared.fixture_seconds,
            "evidence_scope": replay["scope"],
            "execution_mode": replay["execution_mode"],
        },
        "measurements": measurements,
        "continuous_pipeline": replay,
        "memory": memory,
        "failures": failures,
        "network_calls": 0,
        "hot_path_baseline": {
            "status": "unverified",
            "reason": "synthetic_calculation_replay_not_live_scheduler_or_freeze_acceptance",
        },
    }


def _prepare_profiles(
    settings: RuntimeSettings,
    selected: tuple[ScoringProfileId, ...],
    config_path: Path,
    *,
    replay_rows: int | None = None,
) -> _PerformancePreparation:
    workloads: list[PerformanceProfileWorkload] = []
    profiles: list[_ProfileLoad] = []
    inputs: tuple[MarketQuoteInput, ...] = ()
    quotes: tuple[MarketQuote, ...] = ()
    fixture_seconds = 0.0
    cost_policy = LoadExecutionCostPolicy(config_path.parent)
    for profile_id in selected:
        strategy = LoadStrategySettings(settings.strategy_config_path, scoring_profile=profile_id)
        try:
            loaded = LoadScoringProfile(profile_id, training_root=settings.data_paths.models)
        except ScoringModelsUnavailableError as exc:
            profiles.append(_ProfileLoad(profile_id, exc.reason))
            continue
        started = time.perf_counter()
        market, market_quotes, features = BuildPerformanceFixtures(
            settings.performance_budgets, loaded, rows=replay_rows
        )
        fixture_seconds += time.perf_counter() - started
        workloads.append(
            PerformanceProfileWorkload(
                loaded, _recommendation_policy(strategy), features, settings.config_version, cost_policy
            )
        )
        profiles.append(
            _ProfileLoad(
                profile_id,
                None,
                len(features),
                tuple((head.value, runtime.predictor.model_hash) for head, runtime in loaded.heads.items()),
            )
        )
        if not inputs:
            inputs, quotes = market, market_quotes
    return _PerformancePreparation(tuple(workloads), tuple(profiles), inputs, quotes, fixture_seconds)


def _project_profile_loads(profiles: tuple[_ProfileLoad, ...]) -> dict[str, object]:
    return {
        profile.profile_id: {"status": "unverified", "reason": profile.reason}
        if profile.reason is not None
        else {"status": "ready", "input_count": profile.input_count, "models": dict(profile.models)}
        for profile in profiles
    }


def Main(argv: list[str] | None = None) -> int:
    args = BuildParser().parse_args(argv)
    try:
        if args.cpu_profile_dir is not None and (args.replay_ticks != 1 or args.baseline is not None):
            raise ValueError("CPU tracing requires one tick per scenario and no latency baseline")
        if args.cpu_profile_mode == "gc" and args.cpu_profile_dir is None:
            raise ValueError("GC tracing requires --cpu-profile-dir")
        tracing = (
            PerformanceCpuTrace(
                args.cpu_profile_dir / (args.profile or "configured"), functions=args.cpu_profile_mode == "functions"
            )
            if args.cpu_profile_dir
            else nullcontext()
        )
        with tracing as trace:
            report = Run(
                args.config.resolve(),
                baseline_path=args.baseline.resolve() if args.baseline else None,
                scoring_profile=args.profile,
                replay_ticks=args.replay_ticks,
                replay_rows=args.replay_rows,
                trace=trace,
            )
    except ScoringModelsUnavailableError:
        report = _unavailable_report("model_unavailable")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        report = _unavailable_report("workload_invalid")
        report["failure_category"] = type(exc).__name__
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output is not None:
        args.output.write_text(f"{payload}\n", encoding="utf-8")
    print(payload)
    return 0 if report["status"] == "passed" else 1


def _unavailable_report(reason: str) -> dict[str, object]:
    return {
        "schema_version": "production_performance",
        "status": "failed",
        "workload": {},
        "measurements": {},
        "memory": {},
        "failures": [reason],
        "network_calls": 0,
    }


def _operations(
    market_inputs: tuple[MarketQuoteInput, ...],
    market_quotes: tuple[MarketQuote, ...],
    candidates: tuple[FeatureSnapshot, ...],
    candidate_union_fixture: _CandidateUnionFixture,
    context: _OperationContext,
) -> tuple[dict[str, Callable[[], object]], dict[str, str]]:
    config_version = context.config_version
    policy = context.policy
    model_scoring = context.model_scoring
    observed_at = market_quotes[0].received_time
    observations = CompleteRealtimeObservations(market_quotes, observed_at)
    merged = MergeMarketObservations(observations, merge_epoch="performance:market", observed_at=observed_at)
    changed_quotes = tuple(
        replace(
            quote,
            price=(quote.price or 0.0) + 0.01,
            source_time=observed_at + timedelta(milliseconds=1),
            received_time=observed_at + timedelta(milliseconds=1),
            data_version=f"{quote.data_version}:overlay",
        )
        for quote in market_quotes[:18]
    )
    changed_at = observed_at + timedelta(milliseconds=1)
    overlay_snapshot = MergeMarketObservations(
        tuple(
            ObservationFromQuote(quote, source="performance_overlay", observed_at=changed_at)
            for quote in changed_quotes
        ),
        observed_at=changed_at,
        limits=MergeObservationLimits(targeted_codes=tuple(quote.code for quote in changed_quotes)),
        merge_epoch="performance:market",
    )
    committed_overlay = OverlayCanonicalSnapshot(merged, overlay_snapshot, merge_epoch="performance:overlay")
    candidate_union_snapshot = MergeMarketObservations(
        tuple(
            ObservationFromQuote(
                replace(
                    quote,
                    source_time=changed_at,
                    received_time=changed_at,
                    data_version=f"{quote.data_version}:candidate-union",
                ),
                source="performance_candidate_union",
                observed_at=changed_at,
            )
            for quote in candidate_union_fixture.quotes
        ),
        observed_at=changed_at,
        limits=MergeObservationLimits(targeted_codes=candidate_union_fixture.plans.physical_union()),
        merge_epoch="performance:market",
    )
    market_features = tuple(
        replace(candidates[index % len(candidates)], quote=quote) for index, quote in enumerate(market_quotes)
    )
    tomorrow_input = TomorrowNativeInput(
        observed_at.date(),
        "afternoon",
        "performance-data",
        config_version,
        observed_at,
        market_features,
        tuple(feature.quote.code for feature in candidates),
        candidates,
        30.0,
        30.0,
    )
    candidate_input = replace(
        tomorrow_input,
        data_version="performance-candidate-data",
        market_features=candidates,
    )
    local_projection = BuildScoredLocal(
        tomorrow_input,
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=model_scoring),
    )
    reviews = _abstaining_reviews(local_projection, observed_at)
    api_operations = _api_operations(candidates, observed_at)
    overlay_commit = _overlay_cas_operation(candidates, observed_at)

    def tomorrow_projection() -> object:
        return BuildScoredLocal(
            tomorrow_input,
            policy,
            sequence=1,
            runtime=ScoredProjectionInputs(model_scoring=model_scoring),
        )

    def candidate_projection() -> object:
        return BuildScoredLocal(
            candidate_input,
            policy,
            sequence=1,
            runtime=ScoredProjectionInputs(model_scoring=model_scoring),
        )

    def active_score(strategy: Strategy, item: FeatureSnapshot) -> LocalScoreResult:
        board_policy = policy.board_policy(strategy, item.quote.board)
        if board_policy is None:
            raise ValueError(f"missing board policy for {strategy.value}/{item.quote.board.value}")
        return ScoreBoardStrategy(item, board_policy)

    def candidate_preselection() -> object:
        """Exercise both production planners against the complete market population."""

        nonlocal observations
        if observations:
            observations = ()
            gc.collect()
        return BuildCandidatePlans(
            market_features,
            None,
            CandidatePlanningContext(
                evaluated_at=observed_at,
                data_version="performance-candidate-planning",
                policy=policy,
                model_scoring=model_scoring,
            ),
        )

    def candidate_union_projection() -> object:
        if len(candidate_union_fixture.plans.physical_union()) != len(candidate_union_fixture.quotes):
            raise ValueError("candidate union fixture identity mismatch")
        return OverlayCanonicalSnapshot(merged, candidate_union_snapshot, merge_epoch="performance:candidate")

    operations: dict[str, Callable[[], object]] = {
        "market_normalization": lambda: tuple(BuildMarketQuote(item) for item in market_inputs),
        "market_merge": lambda: MergeMarketObservations(
            observations, merge_epoch="performance:market", observed_at=observed_at
        ),
        "canonical_snapshot": lambda: ColumnarQuoteBatch.from_snapshot(
            merged,
            config_version=config_version,
            schema_version="performance-market",
        ),
        "targeted_overlay_commit": lambda: (
            OverlayCanonicalSnapshot(merged, overlay_snapshot, merge_epoch="performance:overlay"),
            TargetedMarketChanges(merged, committed_overlay, tuple(quote.code for quote in changed_quotes)),
            overlay_commit(),
        ),
        "board_preselection": candidate_preselection,
        "candidate_union_projection": candidate_union_projection,
        "board_local_scoring": lambda: tuple(active_score(Strategy.TOMORROW, item) for item in candidates),
        "two_strategy_board_scoring": lambda: tuple(
            tuple(active_score(strategy, item) for strategy in (Strategy.TOMORROW, Strategy.D25)) for item in candidates
        ),
        "three_board_wall_clock": lambda: tuple(
            tuple(active_score(Strategy.TOMORROW, item) for item in candidates if item.quote.board is board)
            for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
        ),
        "global_selection": lambda: sorted(
            (active_score(Strategy.TOMORROW, item).base_score for item in candidates),
            reverse=True,
        )[:6],
        "board_ready_to_draft": candidate_projection,
        "quote_to_draft": tomorrow_projection,
        "deepseek_to_hybrid": lambda: BuildScoredHybrid(
            local_projection,
            policy,
            reviews,
            review_deadline=observed_at + timedelta(minutes=18),
        ),
        **api_operations,
    }
    provenance = {
        "market_normalization": "trader.recommendation.infra.normalization.quote.build_market_quote",
        "market_merge": "trader.recommendation.infra.normalization.merge.merge_market_observations",
        "canonical_snapshot": "trader.recommendation.infra.normalization.columnar.ColumnarQuoteBatch.from_snapshot",
        "targeted_overlay_commit": (
            "trader.recommendation.infra.normalization.merge.overlay_canonical_snapshot + trader."
            "recommendation.application.pipeline.freeze_publish.snapshot_publisher.UnifiedDecisionIndex.publish_overlay"
        ),
        "board_preselection": (
            "trader.recommendation.application.pipeline.candidate_pool.candidate_builder.build_candidate_plans"
        ),
        "candidate_union_projection": (
            "trader.recommendation.application.pipeline.candidate_pool.candidate_builder."
            "CandidatePlanSet.physical_union + "
            "trader.recommendation.infra.normalization.merge.overlay_canonical_snapshot"
        ),
        "board_local_scoring": "trader.recommendation.domain.scoring.scoring.score_board_strategy",
        "two_strategy_board_scoring": "trader.recommendation.domain.scoring.scoring.score_board_strategy",
        "three_board_wall_clock": "trader.recommendation.domain.scoring.scoring.score_board_strategy",
        "global_selection": "trader.recommendation.domain.scoring.scoring.score_board_strategy",
        "board_ready_to_draft": (
            "trader.recommendation.application.pipeline.final_selection.decision_projection.build_scored_local"
        ),
        "quote_to_draft": (
            "trader.recommendation.application.pipeline.final_selection.decision_projection.build_scored_local"
        ),
        "deepseek_to_hybrid": (
            "trader.recommendation.application.pipeline.final_selection.decision_projection.build_scored_hybrid"
        ),
        "sse_publish": (
            "trader.recommendation.application.pipeline.freeze_publish.event_stream."
            "UnifiedDecisionEventStream.publish_committed"
        ),
        "snapshot_api": "trader.http_api.handlers.product_handler._current",
        "etag_api": "trader.http_api.handlers.product_handler._current",
        "dates_api": "trader.http_api.handlers.product_handler._dates",
        "status_api": "trader.http_api.handlers.product_handler._status",
    }
    return operations, provenance


def _api_operations(
    candidates: tuple[FeatureSnapshot, ...],
    observed_at: datetime,
) -> dict[str, Callable[[], object]]:
    index = UnifiedDecisionIndex()
    drafts = UnifiedDecisionDraftIndex()
    stream = UnifiedDecisionEventStream(history_size=256, client_queue_size=16)
    decision = _performance_decision(candidates, observed_at, sequence=1)
    overlay = DecisionOverlay(
        decision.strategy,
        decision.trade_date,
        decision.version,
        decision.observed_at,
        tuple(item.quote for item in decision.items if item.quote is not None),
        sequence=1,
    )
    if not index.publish_scored(decision, overlay, expected_version=None).accepted:
        raise RuntimeError("performance decision setup failed")
    stream.publish_committed(BuildDecisionCommitted(decision))
    queries = UnifiedDecisionQueries(index, drafts, _EmptyHistory(), _FixedClock(observed_at))
    app = CreateApp(
        services=UnifiedWebServices(
            queries,
            stream,
            lambda: {
                "status": "running",
                "runtime_started": True,
                "phase": "afternoon",
                "deepseek_budget": {"used": 0, "remaining": 168},
            },
        )
    )
    client = app.test_client()
    path = "/api/decisions/tomorrow/current"
    etag = client.get(path).headers["ETag"]
    event = BuildDecisionCommitted(decision)
    return {
        "sse_publish": lambda: stream.publish_committed(event),
        "snapshot_api": lambda: client.get(path),
        "etag_api": lambda: client.get(path, headers={"If-None-Match": etag}),
        "dates_api": lambda: client.get("/api/decisions/tomorrow/dates"),
        "status_api": lambda: client.get("/api/status"),
    }


def _overlay_cas_operation(
    candidates: tuple[FeatureSnapshot, ...],
    observed_at: datetime,
) -> Callable[[], object]:
    index = UnifiedDecisionIndex()
    decision = _performance_decision(candidates, observed_at, sequence=1)
    initial = DecisionOverlay(
        decision.strategy,
        decision.trade_date,
        decision.version,
        observed_at,
        tuple(item.quote for item in decision.items if item.quote is not None),
        sequence=1,
    )
    if not index.publish_scored(decision, initial, expected_version=None).accepted:
        raise RuntimeError("performance overlay setup failed")
    counter = 0

    def commit() -> object:
        nonlocal counter
        counter += 1
        current = index.snapshot(decision.strategy).overlay
        if current is None:
            raise RuntimeError("performance overlay is unavailable")
        at = observed_at + timedelta(milliseconds=counter)
        quotes = tuple(
            replace(quote, price=quote.price + counter / 10000.0, source_time=at, data_version=f"overlay:{counter}")
            for quote in current.quotes
        )
        overlay = DecisionOverlay(
            decision.strategy, decision.trade_date, decision.version, at, quotes, sequence=counter + 1
        )
        return index.publish_overlay(overlay, expected_version=current.version)

    return commit


def _performance_decision(
    candidates: tuple[FeatureSnapshot, ...],
    observed_at: datetime,
    *,
    sequence: int,
) -> ScoredDecision:
    items: list[DecisionItem] = []
    for rank, feature in enumerate(candidates[:12], start=1):
        quote = feature.quote
        if quote.price is None:
            raise RuntimeError("performance quote price is unavailable")
        items.append(
            DecisionItem(
                quote.code,
                RecommendationAction.EXECUTABLE if rank <= 6 else RecommendationAction.OBSERVE,
                True,
                rank,
                90.0 - rank,
                85.0 - rank / 10.0,
                85.0 - rank / 10.0,
                (("local_score", 85.0 - rank / 10.0),),
                (),
                "threshold_met" if rank <= 6 else "near_threshold",
                quote.board,
                rank if rank <= 6 else rank - 6,
                quote.name,
                quote.industry,
                DecisionQuote(
                    quote.code,
                    quote.price,
                    quote.pct_change,
                    quote.amount,
                    quote.turnover_rate,
                    quote.market_cap,
                    quote.source,
                    quote.source_time,
                    quote.data_version,
                ),
            )
        )
    return ScoredDecision(
        Strategy.TOMORROW,
        observed_at.date(),
        sequence,
        observed_at,
        "local",
        None,
        (("market", f"performance:{sequence}"),),
        "config:performance",
        "strategy:performance",
        "fusion:performance",
        tuple(items),
        (("hard_filter", 348),),
        population_count=360,
        rejected_count=348,
    )


def _abstaining_reviews(
    projection: ScoredLocalProjection,
    observed_at: datetime,
) -> dict[str, DeepSeekReview]:
    return {
        candidate.code: DeepSeekReview(
            candidate.code,
            ReviewOutcome.ABSTAIN,
            {},
            (),
            observed_at + timedelta(seconds=1),
            error="performance_abstain",
        )
        for candidate in projection.review_candidates
    }


def _measure(operation: Callable[[], object], warmup: int, rounds: int) -> dict[str, object]:
    for _ in range(warmup):
        operation()
    values: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        operation()
        values.append((time.perf_counter() - started) * 1000.0)
    ordered = sorted(values)
    p95 = ordered[max(0, min(len(ordered) - 1, round(len(ordered) * 0.95) - 1))]
    return {
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(p95, 3),
        "maximum_ms": round(max(values), 3),
        "samples": rounds,
    }


def _baseline(path: Path | None, identity: dict[str, object]) -> dict[str, object]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("measurements"), dict):
        raise ValueError("performance baseline must contain measurements")
    previous_identity = raw.get("identity")
    if identity.get("instrumented") or isinstance(previous_identity, dict) and previous_identity.get("instrumented"):
        raise ValueError("instrumented replay cannot be a latency baseline")
    if isinstance(previous_identity, dict) and previous_identity.get("replay_rows") != identity.get("replay_rows"):
        raise ValueError("performance baseline replay population does not match")
    if isinstance(previous_identity, dict) and previous_identity.get("continuous_contract") != identity.get(
        "continuous_contract"
    ):
        raise ValueError("performance baseline continuous contract does not match")
    if isinstance(previous_identity, dict) and previous_identity.get("replay_ticks", 0) != identity.get(
        "replay_ticks", 0
    ):
        raise ValueError("performance baseline replay identity does not match")
    if not isinstance(previous_identity, dict) or any(
        key not in previous_identity or key not in identity or previous_identity[key] != identity[key]
        for key in (
            "config_version",
            "scoring_profile",
            "fixture_sha256",
            "models_sha256",
            "strategy_contracts_sha256",
            "cost_policy_sha256",
            "performance_budgets_sha256",
            "python",
            "dependencies",
            "cpu",
            "cpu_count",
            "machine",
            "system",
            "kernel",
        )
    ):
        raise ValueError("performance baseline input or model identity does not match")
    return cast(dict[str, object], raw["measurements"])


def _failures(
    measurements: dict[str, dict[str, object]],
    budgets: PerformanceBudgetSettings,
    baseline: dict[str, object],
) -> list[str]:
    failures: list[str] = []
    for name, limit in budgets.latency_p95_ms.items():
        if name == "browser_patch_to_paint":
            continue
        measured = measurements.get(name)
        measured_p95 = measured.get("p95_ms") if measured is not None else None
        if not isinstance(measured_p95, (int, float)) or isinstance(measured_p95, bool) or measured_p95 > limit:
            failures.append(f"{name}:absolute_budget")
            continue
        previous = baseline.get(name)
        if isinstance(previous, Mapping) and isinstance(previous.get("p95_ms"), (int, float)):
            allowed = float(previous["p95_ms"]) * (1.0 + budgets.relative_regression_percent / 100.0)
            if measured_p95 > allowed:
                failures.append(f"{name}:relative_regression")
    return failures


def _source_digest(source_root: Path) -> str:
    digest = hashlib.sha256()
    paths = sorted((*source_root.rglob("*.py"), *source_root.rglob("*.js")), key=lambda item: item.as_posix())
    for path in paths:
        digest.update(path.relative_to(source_root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _cpu_identity() -> str:
    if sys.platform.startswith("linux"):
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                return line.partition(":")[2].strip()
    return platform.processor() or platform.machine()


class _FixedClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value


class _EmptyHistory:
    def load(self, strategy: Strategy, trade_date: date) -> CommittedDecisionRecord | None:
        del strategy, trade_date
        return None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        del strategy, limit
        return ()


if __name__ == "__main__":
    sys.exit(Main())
