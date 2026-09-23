"""Typed projection of scored candidate quality into runtime supply diagnostics."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from trader.recommendation.application.pipeline.final_selection.decision_projection import ScoredLocalProjection
from trader.recommendation.application.pipeline.quality_check.input_quality_service import ScoredInputQuality
from trader.recommendation.application.ports.read_only_queries import InputQualityStatus, SupplySummary
from trader.recommendation.domain.evidence.pipeline import (
    PIPELINE_STAGES,
    PipelineFacet,
    PipelineMetricName,
    PipelineMetricRange,
    PipelineReasonCount,
    PipelineStage,
    PipelineStageKey,
    PipelineStageSnapshot,
    PipelineStageState,
    PipelineStageStatus,
    RecommendationPipelineStatus,
    Severity,
    SourceHealth,
    SourceHealthState,
    StageReasonAggregate,
    StageState,
)
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.market.eligibility import IssuerEligibilityBatch
from trader.recommendation.domain.publication.decision_identity import DecisionItem, ScoredDecision
from trader.recommendation.domain.publication.models import (
    RecommendationAction,
    ScoredDisposition,
    ScoredStockEvaluation,
)
from trader.recommendation.domain.selection.scored_selection import (
    ScoredCandidateStageCounts,
    StagePopulationFacts,
    split_filter_reason_counts,
)


@dataclass(frozen=True)
class _PipelineCompletionOptions:
    candidate_quote_eligible: int
    candidate_score_threshold: float | None


def build_supply_status(
    projection: ScoredLocalProjection,
    candidate_stage_counts: ScoredCandidateStageCounts | None = None,
    *,
    candidate_quote_eligible: int | None = None,
    candidate_score_threshold: float | None = None,
    decision: ScoredDecision | None = None,
    issuer_eligibility: IssuerEligibilityBatch | None = None,
) -> InputQualityStatus:
    quality = projection.input_quality
    requested = set(projection.native_input.requested_codes)
    evaluations = tuple(item for item in projection.selection.evaluations if item.code in requested)
    active_decision = decision or projection.local
    diagnostics = active_decision.selection_diagnostics
    if diagnostics is None:
        raise ValueError("scored input status requires selection diagnostics")
    decision_items = active_decision.items
    if candidate_stage_counts is not None:
        stage_counts = candidate_stage_counts
    else:
        # Business-eligible stocks are also input-ready, so the estimate never falls below
        # dynamic eligibility even when pending counts also cover candidate features.
        business_eligible = max(0, quality.population_count - quality.population_rejected_count)
        ready_estimate = max(
            0,
            quality.population_count - quality.data_pending_count - quality.refresh_pending_count,
        )
        stage_counts = ScoredCandidateStageCounts(
            issuer_eligible_population=quality.population_count,
            input_ready_population=max(business_eligible, ready_estimate),
            dynamic_filter_eligible=business_eligible,
            strategy_history_eligible=quality.history_covered_count,
            model_input_eligible=quality.history_covered_count,
            candidate_score_eligible=quality.candidate_scored_count,
            candidate_limit_selected=quality.candidate_scored_count,
        )
    reported_quote_eligible = (
        quality.candidate_feature_count if candidate_quote_eligible is None else candidate_quote_eligible
    )
    quote_eligible = min(stage_counts.candidate_limit_selected, reported_quote_eligible)
    pipeline = _completed_pipeline(
        projection,
        active_decision,
        evaluations,
        stage_counts,
        _PipelineCompletionOptions(quote_eligible, candidate_score_threshold),
    )
    reasons: Counter[str] = Counter()
    for item in evaluations:
        reasons.update(reason.code for reason in item.filter_reasons)
        reasons.update(reason.code for reason in item.optional_flags)
        if item.candidate_audit_pruning_reason:
            reasons[item.candidate_audit_pruning_reason] += 1
        if item.selection_skip_reason:
            reasons[item.selection_skip_reason] += 1
    reasons.update(item.reason for item in decision_items if item.reason)
    reasons.update(risk for item in decision_items for risk in item.risk_codes)
    first_nine = build_first_nine_stage_snapshots(
        stage_counts,
        batch_id=projection.native_input.input_version,
        as_of=projection.local.observed_at,
        population_count=quality.population_count,
        candidate_feature_count=quality.candidate_feature_count,
        data_pending_count=quality.data_pending_count,
        refresh_pending_count=quality.refresh_pending_count,
        degraded_reasons=quality.degraded_reasons,
        issuer_eligibility=issuer_eligibility,
        quality_ready_count=quality.candidate_scored_count,
    )
    return InputQualityStatus(
        strategy=projection.local.strategy,
        status=quality.status,
        publishable=quality.publishable,
        summary=_supply_summary(projection, decision=active_decision),
        pipeline=pipeline,
        stage_snapshots=build_complete_stage_snapshots(first_nine, pipeline),
        population_count=quality.population_count,
        candidate_count=quality.candidate_count,
        candidate_feature_count=quality.candidate_feature_count,
        population_rejected_count=quality.population_rejected_count,
        candidate_rejected_count=quality.candidate_rejected_count,
        candidate_scored_count=quality.candidate_scored_count,
        security_master_covered_count=quality.security_master_covered_count,
        history_covered_count=quality.history_covered_count,
        history_required_sessions=quality.history_required_sessions,
        candidate_feature_coverage_ratio=quality.candidate_feature_coverage_ratio,
        security_master_coverage_ratio=quality.security_master_coverage_ratio,
        history_coverage_ratio=quality.history_coverage_ratio,
        population_filter_reason_counts=tuple(quality.population_filter_reason_counts.items()),
        candidate_filter_reason_counts=tuple(quality.candidate_filter_reason_counts.items()),
        candidate_transient_reason_counts=tuple(quality.candidate_transient_reason_counts.items()),
        candidate_optional_reason_counts=tuple(quality.candidate_optional_reason_counts.items()),
        degraded_reasons=quality.degraded_reasons,
        data_pending_count=quality.data_pending_count,
        refresh_pending_count=quality.refresh_pending_count,
        supply_reason_counts=tuple(reasons.items()),
        primary_blocker=_primary_supply_blocker(quality, pipeline, empty_reason=diagnostics.empty_reason),
    )


def build_first_nine_stage_snapshots(
    stage_counts: ScoredCandidateStageCounts,
    *,
    batch_id: str,
    as_of: datetime,
    population_count: int,
    candidate_feature_count: int,
    data_pending_count: int = 0,
    refresh_pending_count: int = 0,
    degraded_reasons: tuple[str, ...] = (),
    issuer_eligibility: IssuerEligibilityBatch | None = None,
    quality_ready_count: int | None = None,
) -> tuple[PipelineStageSnapshot, ...]:
    """Project the active first-nine-stage counters without mixing readiness and rejection."""

    raw_population_count = issuer_eligibility.input_count if issuer_eligibility is not None else population_count
    registry_eligible_count = issuer_eligibility.eligible_count if issuer_eligibility is not None else population_count
    issuer_count = min(registry_eligible_count, stage_counts.issuer_eligible_population)
    static_pending = 0
    static_rejected = max(0, raw_population_count - issuer_count - static_pending)
    static_reason_counts = Counter(
        {item.reason.value: item.count for item in issuer_eligibility.reason_counts}
        if issuer_eligibility is not None
        else {}
    )
    unexplained_static_rejections = static_rejected - sum(static_reason_counts.values())
    if unexplained_static_rejections > 0:
        static_reason_counts["stable_rejected"] += unexplained_static_rejections
    dynamic_gap = max(0, issuer_count - stage_counts.input_ready_population)
    dynamic_refresh_pending = min(dynamic_gap, refresh_pending_count)
    dynamic_data_pending = min(dynamic_gap - dynamic_refresh_pending, data_pending_count)
    dynamic_pending = dynamic_refresh_pending + dynamic_data_pending
    dynamic_failed = max(0, dynamic_gap - dynamic_pending)
    dynamic_rejected = max(0, stage_counts.input_ready_population - stage_counts.dynamic_filter_eligible)
    candidate_output = stage_counts.candidate_limit_selected
    quality_output = min(
        candidate_output,
        candidate_feature_count,
        candidate_output if quality_ready_count is None else quality_ready_count,
    )
    quality_pending = max(0, candidate_output - quality_output)
    source_degraded = bool(degraded_reasons)
    source_health = SourceHealth(
        SourceHealthState.DEGRADED if source_degraded else SourceHealthState.READY,
        source_count=1,
        healthy_source_count=1,
        latest_success_at=as_of,
        age_seconds=0.0,
    )
    legacy_rows = (
        (PipelineStage.DATA_SOURCE, raw_population_count, raw_population_count, 0, 0, 0, ()),
        (PipelineStage.STATIC_MARKET, raw_population_count, raw_population_count, 0, 0, 0, ()),
        (PipelineStage.STATIC_STANDARDIZE, raw_population_count, raw_population_count, 0, 0, 0, ()),
        (
            PipelineStage.STATIC_FILTER,
            raw_population_count,
            issuer_count,
            static_rejected,
            static_pending,
            0,
            _stage_reasons(("data_pending", static_pending), *tuple(sorted(static_reason_counts.items()))),
        ),
        (
            PipelineStage.DYNAMIC_MARKET,
            issuer_count,
            stage_counts.input_ready_population,
            0,
            dynamic_pending,
            dynamic_failed,
            _stage_reasons(
                ("data_pending", dynamic_data_pending),
                ("refresh_pending", dynamic_refresh_pending),
                ("source_failed", dynamic_failed),
            ),
        ),
        (
            PipelineStage.DYNAMIC_STANDARDIZE,
            stage_counts.input_ready_population,
            stage_counts.input_ready_population,
            0,
            0,
            0,
            (),
        ),
        (
            PipelineStage.DYNAMIC_FILTER,
            stage_counts.input_ready_population,
            stage_counts.dynamic_filter_eligible,
            dynamic_rejected,
            0,
            0,
            _stage_reasons(("dynamic_rejected", dynamic_rejected)),
        ),
        (
            PipelineStage.CANDIDATE_POOL,
            stage_counts.dynamic_filter_eligible,
            candidate_output,
            0,
            0,
            0,
            _stage_reasons(
                ("board_limit", max(0, stage_counts.dynamic_filter_eligible - candidate_output)),
            ),
        ),
        (
            PipelineStage.QUALITY_CHECK,
            candidate_output,
            quality_output,
            0,
            quality_pending,
            0,
            _stage_reasons(("quality_pending", quality_pending)),
        ),
    )
    facts = stage_counts.stage_facts
    if facts:
        facts = dict(facts)
        if quality_ready_count is not None and "quality_check" in facts:
            quality_input = facts["quality_check"].input_count
            quality_output = min(quality_input, quality_ready_count)
            facts["quality_check"] = StagePopulationFacts(
                quality_input,
                quality_output,
                pending_count=quality_input - quality_output,
                reasons={"quality_pending": quality_input - quality_output}
                if quality_input > quality_output
                else {},
            )
        rows = tuple(
            (
                stage,
                facts[stage.value].input_count,
                facts[stage.value].output_count,
                facts[stage.value].rejected_count,
                facts[stage.value].pending_count,
                facts[stage.value].failed_count,
                _stage_reasons(*tuple(sorted(facts[stage.value].reasons.items()))),
            )
            for stage in PIPELINE_STAGES[:9]
            if stage.value in facts
        )
        if len(rows) != 9:
            raise ValueError("production stage facts must cover the first nine stages")
    else:
        rows = legacy_rows
    snapshots: list[PipelineStageSnapshot] = []
    input_batch_id = batch_id
    for stage, input_count, output_count, rejected_count, pending_count, failed_count, reasons in rows:
        output_batch_id = f"{batch_id}:{stage.value}"
        snapshots.append(
            _stage_snapshot(
                stage,
                input_batch_id=input_batch_id,
                output_batch_id=output_batch_id,
                as_of=as_of,
                input_count=input_count,
                output_count=output_count,
                rejected_count=rejected_count,
                pending_count=pending_count,
                failed_count=failed_count,
                reasons=reasons,
                source_health=source_health,
                source_degraded=source_degraded and stage is PipelineStage.DATA_SOURCE,
            )
        )
        input_batch_id = output_batch_id
    return tuple(snapshots)


def build_complete_stage_snapshots(
    first_nine: tuple[PipelineStageSnapshot, ...],
    pipeline: RecommendationPipelineStatus,
) -> tuple[PipelineStageSnapshot, ...]:
    """Join the input chain to the five scored runtime stages without inventing counts."""

    if tuple(item.stage for item in first_nine) != PIPELINE_STAGES[:9]:
        raise ValueError("complete pipeline snapshots require the ordered first nine stages")
    previous = first_nine[-1]
    batch_root = first_nine[0].input_batch_id
    stage_groups = (
        (
            PipelineStage.LOCAL_SCORE,
            (pipeline.stage("evidence_score"), pipeline.stage("model_cost_gate"), pipeline.stage("local_score")),
        ),
        (
            PipelineStage.RISK_REVIEW,
            (pipeline.stage("deepseek_review"),),
        ),
        (
            PipelineStage.SCORE_MERGE,
            (pipeline.stage("fusion"),),
        ),
        (
            PipelineStage.DOWNSIDE_ACTION,
            (pipeline.stage("action_gate"),),
        ),
        (
            PipelineStage.FINAL_SELECTION,
            (pipeline.stage("concentration"),),
        ),
    )
    snapshots = list(first_nine)
    for stage, statuses in stage_groups:
        output_count = _runtime_output_count(stage, statuses, previous.output_count)
        pending_count = previous.output_count if output_count == 0 and _runtime_stage_pending(statuses) else 0
        state = _runtime_stage_state(statuses, previous.output_count, output_count)
        output_batch_id = f"{batch_root}:{stage.value}"
        snapshot = PipelineStageSnapshot(
            stage=stage,
            stage_order=PIPELINE_STAGES.index(stage) + 1,
            input_batch_id=previous.output_batch_id,
            output_batch_id=output_batch_id,
            as_of=previous.as_of,
            state=state,
            input_count=previous.output_count,
            output_count=output_count,
            rejected_count=0,
            pending_count=pending_count,
            failed_count=0,
            reasons=_runtime_stage_reasons(statuses),
            source_health=previous.source_health,
            latency_ms=round(sum(item.duration_ms or 0.0 for item in statuses)),
            degraded=state is StageState.DEGRADED,
        )
        snapshots.append(snapshot)
        previous = snapshot
    return tuple(snapshots)


def _runtime_output_count(
    stage: PipelineStage,
    statuses: tuple[PipelineStageStatus, ...],
    previous_output_count: int,
) -> int:
    if stage is PipelineStage.RISK_REVIEW:
        return previous_output_count
    output = statuses[-1].output_count
    if output is None:
        return 0
    return min(previous_output_count, output)


def _runtime_stage_pending(statuses: tuple[PipelineStageStatus, ...]) -> bool:
    return any(item.state in {"pending", "running"} for item in statuses)


def _runtime_stage_state(
    statuses: tuple[PipelineStageStatus, ...],
    input_count: int,
    output_count: int,
) -> StageState:
    # A zero-input downstream stage was not executed; do not present it as a
    # successful empty result. A legitimate empty selection still has a
    # positive input count and remains ready.
    if input_count == 0 and output_count == 0 and not _runtime_stage_pending(statuses):
        return StageState.NOT_READY
    if not output_count and _runtime_stage_pending(statuses):
        return StageState.NOT_READY
    if any(item.state == "degraded" for item in statuses) or _runtime_stage_pending(statuses):
        return StageState.DEGRADED
    return StageState.READY


def _runtime_stage_reasons(
    statuses: tuple[PipelineStageStatus, ...],
) -> tuple[StageReasonAggregate, ...]:
    counts: Counter[str] = Counter()
    for status in statuses:
        counts.update({item.reason: item.count for item in status.reason_counts})
    return _stage_reasons(*tuple(sorted(counts.items())))


def _stage_snapshot(
    stage: PipelineStage,
    *,
    input_batch_id: str,
    output_batch_id: str,
    as_of: datetime,
    input_count: int,
    output_count: int,
    rejected_count: int,
    pending_count: int,
    failed_count: int,
    reasons: tuple[StageReasonAggregate, ...],
    source_health: SourceHealth,
    source_degraded: bool,
) -> PipelineStageSnapshot:
    state = _stage_state(input_count, output_count, pending_count, failed_count, source_degraded)
    return PipelineStageSnapshot(
        stage=stage,
        stage_order=PIPELINE_STAGES.index(stage) + 1,
        input_batch_id=input_batch_id,
        output_batch_id=output_batch_id,
        as_of=as_of,
        state=state,
        input_count=input_count,
        output_count=output_count,
        rejected_count=rejected_count,
        pending_count=pending_count,
        failed_count=failed_count,
        reasons=reasons,
        source_health=source_health,
        latency_ms=0,
        degraded=state is StageState.DEGRADED,
    )


def _stage_reasons(*values: tuple[str, int]) -> tuple[StageReasonAggregate, ...]:
    return tuple(
        StageReasonAggregate(code, code.replace("_", " "), count, Severity.WARNING)
        for code, count in values
        if count > 0
    )


def _stage_state(
    input_count: int,
    output_count: int,
    pending_count: int,
    failed_count: int,
    degraded: bool,
) -> StageState:
    # Preserve the distinction between an empty result and a stage that never
    # received an input batch.
    if input_count == 0 and output_count == 0 and not pending_count and not failed_count:
        return StageState.NOT_READY
    if failed_count and not output_count:
        return StageState.FAILED
    if not output_count and pending_count:
        return StageState.NOT_READY
    if degraded or pending_count or failed_count:
        return StageState.DEGRADED
    return StageState.READY


def build_pending_pipeline(
    stage_counts: ScoredCandidateStageCounts,
    *,
    candidate_feature_count: int,
    primary_blocker: str,
    candidate_score_threshold: float,
) -> RecommendationPipelineStatus:
    refreshing = primary_blocker == "candidate_quotes_pending"
    refresh_output = None if refreshing else candidate_feature_count
    refresh_state: PipelineStageState = "running" if refreshing else "completed"
    current_stage: PipelineStageKey = "candidate_refresh" if refreshing else "evidence_score"
    pending = tuple(
        PipelineStageStatus(key, "pending", None, None)
        for key in (
            "evidence_score",
            "model_cost_gate",
            "local_score",
            "deepseek_review",
            "fusion",
            "action_gate",
            "concentration",
        )
    )
    return RecommendationPipelineStatus(
        current_stage=current_stage,
        stages=(
            PipelineStageStatus(
                "input_readiness",
                "completed",
                stage_counts.issuer_eligible_population,
                stage_counts.input_ready_population,
            ),
            PipelineStageStatus(
                "dynamic_filter",
                "completed",
                stage_counts.input_ready_population,
                stage_counts.dynamic_filter_eligible,
            ),
            PipelineStageStatus(
                "board_cross_section",
                "completed",
                stage_counts.dynamic_filter_eligible,
                stage_counts.dynamic_filter_eligible,
            ),
            PipelineStageStatus(
                "strategy_history",
                "completed",
                stage_counts.dynamic_filter_eligible,
                stage_counts.strategy_history_eligible,
            ),
            PipelineStageStatus(
                "model_input",
                "completed",
                stage_counts.strategy_history_eligible,
                stage_counts.model_input_eligible,
            ),
            PipelineStageStatus(
                "candidate_score",
                "completed",
                stage_counts.model_input_eligible,
                stage_counts.candidate_score_eligible,
                threshold=candidate_score_threshold,
            ),
            PipelineStageStatus(
                "board_limit",
                "completed",
                stage_counts.candidate_score_eligible,
                stage_counts.candidate_limit_selected,
            ),
            PipelineStageStatus(
                "candidate_refresh",
                refresh_state,
                stage_counts.candidate_limit_selected,
                refresh_output,
            ),
            PipelineStageStatus(
                "input_coverage",
                "pending",
                stage_counts.candidate_limit_selected,
                None,
                facets=(
                    PipelineFacet(
                        "candidate_features",
                        candidate_feature_count,
                        stage_counts.candidate_limit_selected,
                    ),
                ),
            ),
            *pending,
        ),
    )


def update_supply_status_decision(
    current: InputQualityStatus,
    projection: ScoredLocalProjection,
    decision: ScoredDecision,
    *,
    candidate_score_threshold: float,
) -> InputQualityStatus:
    pipeline = current.pipeline
    readiness = pipeline.stage("input_readiness")
    dynamic = pipeline.stage("dynamic_filter")
    history = pipeline.stage("strategy_history")
    model = pipeline.stage("model_input")
    candidate = pipeline.stage("candidate_score")
    board_limit = pipeline.stage("board_limit")
    refresh = pipeline.stage("candidate_refresh")
    stage_counts = ScoredCandidateStageCounts(
        issuer_eligible_population=_required_count(readiness.input_count),
        input_ready_population=_required_count(readiness.output_count),
        dynamic_filter_eligible=_required_count(dynamic.output_count),
        strategy_history_eligible=_required_count(history.output_count),
        model_input_eligible=_required_count(model.output_count),
        candidate_score_eligible=_required_count(candidate.output_count),
        candidate_limit_selected=_required_count(board_limit.output_count),
    )
    return build_supply_status(
        projection,
        stage_counts,
        candidate_quote_eligible=refresh.output_count,
        candidate_score_threshold=candidate_score_threshold,
        decision=decision,
    )


def _completed_pipeline(
    projection: ScoredLocalProjection,
    decision: ScoredDecision,
    evaluations: tuple[ScoredStockEvaluation, ...],
    stage_counts: ScoredCandidateStageCounts,
    options: _PipelineCompletionOptions,
) -> RecommendationPipelineStatus:
    quality = projection.input_quality
    candidate_quote_eligible = options.candidate_quote_eligible
    candidate_score_threshold = options.candidate_score_threshold
    diagnostics = decision.selection_diagnostics
    if diagnostics is None:
        raise ValueError("recommendation pipeline requires selection diagnostics")
    decision_items = decision.items
    model_diagnostics = tuple(item.model_diagnostics for item in decision_items if item.model_diagnostics is not None)
    model_positive = sum(item.predicted_net_excess_pct > 0.0 for item in model_diagnostics)
    action_eligible = sum(item.action is not RecommendationAction.UNAVAILABLE for item in decision_items)
    selected_executable = sum(
        item.selected and item.action is RecommendationAction.EXECUTABLE for item in decision_items
    )
    selected_observe = sum(item.selected and item.action is RecommendationAction.OBSERVE for item in decision_items)
    evaluated_count = len(evaluations)
    disposition_facets = (
        PipelineFacet(
            "filter_pass", sum(item.disposition is ScoredDisposition.PASS for item in evaluations), evaluated_count
        ),
        PipelineFacet(
            "filter_observe",
            sum(item.disposition is ScoredDisposition.OBSERVE_ONLY for item in evaluations),
            evaluated_count,
        ),
        PipelineFacet(
            "filter_reject",
            sum(item.disposition is ScoredDisposition.REJECT for item in evaluations),
            evaluated_count,
        ),
    )
    fusion_applied = sum(_component(item, "deepseek_score") is not None for item in decision_items)
    review_completed = sum(item.review_outcome is not None for item in decision_items)
    deepseek_ranges = _metric_ranges(
        (
            ("deepseek_score", (_component(item, "deepseek_score") for item in decision_items)),
            ("deepseek_risk_penalty", (_component(item, "deepseek_risk_penalty") for item in decision_items)),
        )
    )
    deepseek_state: PipelineStageState = (
        "completed" if decision.stage == "hybrid" else "pending" if projection.review_candidates else "not_applicable"
    )
    deepseek_output = review_completed if decision.stage == "hybrid" else None if projection.review_candidates else 0
    current_stage: PipelineStageKey = (
        "concentration" if decision.stage == "hybrid" or not projection.review_candidates else "deepseek_review"
    )
    business_reason_counts, readiness_reason_counts = split_filter_reason_counts(
        quality.population_filter_reason_counts
    )
    input_readiness_state: PipelineStageState = "degraded" if readiness_reason_counts else "completed"
    candidate_refresh_state: PipelineStageState = (
        "degraded" if candidate_quote_eligible < stage_counts.candidate_limit_selected else "completed"
    )
    return RecommendationPipelineStatus(
        current_stage=current_stage,
        stages=(
            PipelineStageStatus(
                "input_readiness",
                input_readiness_state,
                stage_counts.issuer_eligible_population,
                stage_counts.input_ready_population,
                reason_counts=_reasons(_expand_reason_counts(readiness_reason_counts)),
            ),
            PipelineStageStatus(
                "dynamic_filter",
                "completed",
                stage_counts.input_ready_population,
                stage_counts.dynamic_filter_eligible,
                reason_counts=_reasons(_expand_reason_counts(business_reason_counts)),
            ),
            PipelineStageStatus(
                "board_cross_section",
                "degraded"
                if any(item.disposition is ScoredDisposition.OBSERVE_ONLY for item in evaluations)
                else "completed",
                stage_counts.dynamic_filter_eligible,
                stage_counts.dynamic_filter_eligible,
                metric_ranges=_metric_ranges(
                    (("board_reliability", (item.features.board_data_reliability for item in evaluations)),)
                ),
                facets=disposition_facets,
                reason_counts=_reasons(flag.code for item in evaluations for flag in item.optional_flags),
            ),
            PipelineStageStatus(
                "strategy_history",
                "completed",
                stage_counts.dynamic_filter_eligible,
                stage_counts.strategy_history_eligible,
                metric_ranges=_metric_ranges(
                    (("history_sessions", (float(item.features.history_days) for item in evaluations)),)
                ),
                threshold=float(quality.history_required_sessions),
                reason_counts=_selection_reasons(evaluations, {"strategy_history_insufficient"}),
            ),
            PipelineStageStatus(
                "model_input",
                "completed",
                stage_counts.strategy_history_eligible,
                stage_counts.model_input_eligible,
                metric_ranges=_metric_ranges(
                    (
                        (
                            "input_completeness",
                            (
                                1.0 - item.candidate_missing_ratio
                                for item in evaluations
                                if item.candidate_missing_ratio is not None
                            ),
                        ),
                    )
                ),
                reason_counts=_selection_reasons(evaluations, {"production_model_features_missing"}),
            ),
            PipelineStageStatus(
                "candidate_score",
                "completed",
                stage_counts.model_input_eligible,
                stage_counts.candidate_score_eligible,
                metric_ranges=_metric_ranges((("candidate_score", (item.candidate_score for item in evaluations)),)),
                threshold=candidate_score_threshold,
                reason_counts=_selection_reasons(
                    evaluations,
                    {"candidate_core_missing", "candidate_score_below_minimum"},
                ),
            ),
            PipelineStageStatus(
                "board_limit",
                "completed",
                stage_counts.candidate_score_eligible,
                stage_counts.candidate_limit_selected,
                facets=_board_facets(evaluations),
                reason_counts=_selection_reasons(evaluations, {"board_candidate_limit"}),
            ),
            PipelineStageStatus(
                "candidate_refresh",
                candidate_refresh_state,
                stage_counts.candidate_limit_selected,
                candidate_quote_eligible,
                metric_ranges=_quote_age_ranges(projection),
                reason_counts=_reasons(_expand_reason_counts(quality.candidate_transient_reason_counts)),
            ),
            PipelineStageStatus(
                "input_coverage",
                "degraded"
                if min(
                    quality.candidate_feature_count,
                    quality.security_master_covered_count,
                    quality.history_covered_count,
                )
                < quality.candidate_count
                else "completed",
                quality.candidate_count,
                None,
                facets=(
                    PipelineFacet("candidate_features", quality.candidate_feature_count, quality.candidate_count),
                    PipelineFacet("security_master", quality.security_master_covered_count, quality.candidate_count),
                    PipelineFacet("history", quality.history_covered_count, quality.candidate_count),
                ),
                reason_counts=_reasons(
                    (
                        *_expand_reason_counts(quality.candidate_filter_reason_counts),
                        *_expand_reason_counts(quality.candidate_optional_reason_counts),
                    )
                ),
            ),
            PipelineStageStatus(
                "evidence_score",
                "completed",
                quality.candidate_feature_count,
                quality.candidate_scored_count,
                metric_ranges=_metric_ranges((("base_score", (item.local_base_score for item in evaluations)),)),
            ),
            PipelineStageStatus(
                "model_cost_gate",
                "completed" if model_diagnostics else "not_applicable",
                quality.candidate_scored_count,
                model_positive if model_diagnostics else quality.candidate_scored_count,
                metric_ranges=_metric_ranges(
                    (
                        ("model_signal_score", (item.signal_score for item in model_diagnostics)),
                        (
                            "predicted_excess_return_pct",
                            (item.predicted_excess_return_pct for item in model_diagnostics),
                        ),
                        ("estimated_cost_pct", (item.estimated_cost_pct for item in model_diagnostics)),
                        (
                            "predicted_net_excess_pct",
                            (item.predicted_net_excess_pct for item in model_diagnostics),
                        ),
                        ("model_disagreement_pct", (item.model_disagreement_pct for item in model_diagnostics)),
                    )
                ),
                reason_counts=_reasons(
                    item.reason for item in decision_items if item.reason == "model_net_utility_non_positive"
                ),
            ),
            PipelineStageStatus(
                "local_score",
                "completed",
                quality.candidate_scored_count,
                len(decision_items),
                metric_ranges=_metric_ranges(
                    (
                        ("local_risk_penalty", (item.local_risk_penalty for item in evaluations)),
                        ("local_score", (item.local_score for item in evaluations)),
                    )
                ),
                reason_counts=_selection_reasons(
                    evaluations,
                    {"local_risk_veto", "local_score_below_minimum"},
                ),
            ),
            PipelineStageStatus(
                "deepseek_review",
                deepseek_state,
                len(projection.review_candidates),
                deepseek_output,
                metric_ranges=deepseek_ranges,
                facets=(PipelineFacet("review_completed", review_completed, len(projection.review_candidates)),),
            ),
            PipelineStageStatus(
                "fusion",
                "completed",
                len(decision_items),
                len(decision_items),
                metric_ranges=_metric_ranges((("final_score", (item.final_score for item in decision_items)),)),
                facets=(
                    PipelineFacet("local_only", len(decision_items) - fusion_applied, len(decision_items)),
                    PipelineFacet("hybrid", fusion_applied, len(decision_items)),
                ),
            ),
            PipelineStageStatus(
                "action_gate",
                "completed",
                len(decision_items),
                action_eligible,
                threshold=diagnostics.executable_threshold,
                facets=(
                    PipelineFacet(
                        "executable_threshold_met",
                        sum(item.final_score >= diagnostics.executable_threshold for item in decision_items),
                        len(decision_items),
                    ),
                    PipelineFacet(
                        "observation_threshold_met",
                        sum(item.final_score >= diagnostics.observation_floor for item in decision_items),
                        len(decision_items),
                    ),
                    PipelineFacet(
                        "action_executable",
                        sum(item.action is RecommendationAction.EXECUTABLE for item in decision_items),
                        len(decision_items),
                    ),
                    PipelineFacet(
                        "action_observe",
                        sum(item.action is RecommendationAction.OBSERVE for item in decision_items),
                        len(decision_items),
                    ),
                    PipelineFacet(
                        "action_unavailable",
                        sum(item.action is RecommendationAction.UNAVAILABLE for item in decision_items),
                        len(decision_items),
                    ),
                ),
                reason_counts=_reasons(item.reason for item in decision_items if item.reason),
            ),
            PipelineStageStatus(
                "concentration",
                "completed",
                action_eligible,
                selected_executable + selected_observe,
                facets=(
                    PipelineFacet("selected_executable", selected_executable, action_eligible),
                    PipelineFacet("selected_observe", selected_observe, action_eligible),
                ),
                reason_counts=_reasons(
                    item.reason
                    for item in decision_items
                    if item.reason
                    in {
                        "top_k_limit",
                        "observation_limit",
                        "board_concentration_limit",
                        "industry_limit",
                    }
                ),
            ),
        ),
    )


def _supply_summary(projection: ScoredLocalProjection, *, decision: ScoredDecision | None = None) -> SupplySummary:
    requested = set(projection.native_input.requested_codes)
    features = tuple(
        feature for feature in projection.native_input.candidate_features if feature.quote.code in requested
    )
    complete_quotes = tuple(feature.quote for feature in features if _summary_quote_complete(feature))
    latest = max(
        complete_quotes,
        key=lambda quote: (quote.source_time, quote.received_time, quote.data_version),
        default=None,
    )
    highest = max((item.final_score for item in (decision or projection.local).items), default=None)
    total = projection.input_quality.candidate_count
    return SupplySummary(
        trade_date=projection.local.trade_date,
        quote_total_count=total,
        quote_covered_count=len(complete_quotes),
        quote_missing_count=max(0, total - len(complete_quotes)),
        security_identity_missing_count=max(
            0,
            total - projection.input_quality.security_master_covered_count,
        ),
        latest_quote_source=latest.source if latest is not None else None,
        latest_quote_source_time=latest.source_time if latest is not None else None,
        highest_final_score=highest,
    )


def _summary_quote_complete(feature: FeatureSnapshot) -> bool:
    quote = feature.quote
    return (
        quote.price is not None
        and math.isfinite(quote.price)
        and quote.price > 0.0
        and quote.pct_change is not None
        and math.isfinite(quote.pct_change)
        and bool(quote.source.strip())
        and quote.source_time.tzinfo is not None
        and quote.source_time.utcoffset() is not None
    )


def _primary_supply_blocker(
    quality: ScoredInputQuality,
    pipeline: RecommendationPipelineStatus,
    *,
    empty_reason: str | None,
) -> str:
    action_stage = pipeline.stage("action_gate")
    action_observe = _facet_count(action_stage, "action_observe")
    action_executable = _facet_count(action_stage, "action_executable")
    selected_executable = _facet_count(pipeline.stage("concentration"), "selected_executable")
    full_scored = pipeline.stage("evidence_score").output_count or 0
    strategy_history = pipeline.stage("strategy_history").output_count or 0
    dynamic_filter = pipeline.stage("dynamic_filter").output_count or 0
    model_input = pipeline.stage("model_input").output_count or 0
    requested_candidates = pipeline.stage("candidate_refresh").input_count or 0
    review_eligible = pipeline.stage("deepseek_review").input_count or 0
    action_blocker = "no_executable_candidates" if action_observe else "local_score_below_observation_floor"
    population_reasons = dict(quality.population_filter_reason_counts)
    stale_population = population_reasons.get("stale_quote", 0)
    missing_liquidity_history = population_reasons.get("missing_liquidity_history", 0)
    priorities = (
        (
            quality.candidate_count > 0 and quality.candidate_feature_coverage_ratio < 1.0,
            "candidate_feature_coverage_incomplete",
        ),
        (
            quality.candidate_count > 0 and quality.security_master_coverage_ratio < 1.0,
            "security_master_coverage_incomplete",
        ),
        (
            quality.status == "transient_invalid_empty"
            and requested_candidates == 0
            and stale_population > 0
            and stale_population * 2 >= quality.population_count,
            "market_population_stale",
        ),
        (
            quality.status == "transient_invalid_empty"
            and requested_candidates == 0
            and missing_liquidity_history > 0
            and missing_liquidity_history * 2 >= quality.population_count,
            "market_liquidity_history_unavailable",
        ),
        (
            quality.status == "transient_invalid_empty" and full_scored == 0 and strategy_history < dynamic_filter,
            "strategy_history_unavailable",
        ),
        (
            full_scored == 0 and strategy_history > 0 and model_input == 0,
            "model_input_unavailable",
        ),
        (full_scored == 0, "no_scored_candidates"),
        (empty_reason == "no_positive_net_utility", "no_positive_net_utility"),
        (review_eligible == 0, "no_review_eligible_candidates"),
        (action_executable == 0, action_blocker),
        (selected_executable == 0, "selection_constraints"),
    )
    return next((reason for blocked, reason in priorities if blocked), "ready")


def _metric_ranges(
    groups: Iterable[tuple[PipelineMetricName, Iterable[float | None]]],
) -> tuple[PipelineMetricRange, ...]:
    result: list[PipelineMetricRange] = []
    for metric, raw_values in groups:
        values = tuple(float(value) for value in raw_values if value is not None and math.isfinite(float(value)))
        if values:
            result.append(PipelineMetricRange(metric, min(values), max(values)))
    return tuple(result)


def _reasons(values: Iterable[str]) -> tuple[PipelineReasonCount, ...]:
    return tuple(PipelineReasonCount(reason, count) for reason, count in sorted(Counter(values).items()) if reason)


def _expand_reason_counts(values: Mapping[str, int]) -> tuple[str, ...]:
    return tuple(reason for reason, count in values.items() for _index in range(count))


def _selection_reasons(
    evaluations: tuple[ScoredStockEvaluation, ...],
    accepted: set[str],
) -> tuple[PipelineReasonCount, ...]:
    return _reasons(item.selection_skip_reason for item in evaluations if item.selection_skip_reason in accepted)


def _board_facets(evaluations: tuple[ScoredStockEvaluation, ...]) -> tuple[PipelineFacet, ...]:
    counts = Counter(
        item.features.quote.board.value
        for item in evaluations
        if item.candidate_rank > 0 and item.local_score is not None
    )
    total = sum(counts.values())
    return tuple(PipelineFacet(f"board_{board}", count, total) for board, count in sorted(counts.items()))


def _quote_age_ranges(projection: ScoredLocalProjection) -> tuple[PipelineMetricRange, ...]:
    evaluated_at = projection.native_input.evaluated_at
    return _metric_ranges(
        (
            (
                "quote_age_seconds",
                (
                    max(0.0, (evaluated_at - feature.quote.source_time).total_seconds())
                    for feature in projection.native_input.candidate_features
                ),
            ),
        )
    )


def _component(item: DecisionItem, name: str) -> float | None:
    return next((value for key, value in item.score_components if key == name), None)


def _facet_count(stage: PipelineStageStatus, key: str) -> int:
    return next((item.count for item in stage.facets if item.key == key), 0)


def _required_count(value: int | None) -> int:
    if value is None:
        raise ValueError("completed recommendation pipeline count is missing")
    return value


__all__ = [
    "build_complete_stage_snapshots",
    "build_first_nine_stage_snapshots",
    "build_pending_pipeline",
    "build_supply_status",
    "update_supply_status_decision",
]
