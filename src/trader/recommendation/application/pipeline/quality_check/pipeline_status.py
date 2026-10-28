"""Typed projection of scored candidate quality into runtime supply diagnostics."""

from __future__ import annotations

import math
from collections import Counter

from trader.recommendation.application.pipeline.final_selection.freeze_projection import ScoredLocalProjection
from trader.recommendation.application.pipeline.final_selection.scored_stage_chain import ScoredStageChain
from trader.recommendation.application.pipeline.quality_check.input_quality_service import ScoredInputQuality
from trader.recommendation.application.pipeline.stage_output import StageOutput
from trader.recommendation.application.ports.read_only_queries import InputQualityStatus, SupplySummary
from trader.recommendation.domain.evidence.pipeline import (
    PIPELINE_STAGE_ORDER,
    PIPELINE_STAGES,
    PipelineFacet,
    PipelineReasonCount,
    PipelineStage,
    PipelineStageKey,
    PipelineStageSnapshot,
    PipelineStageStatus,
    RecommendationPipelineStatus,
    ValidateStageBatchContinuity,
)
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.freeze_identity import ScoredFreeze
from trader.recommendation.domain.publication.models import (
    RecommendationAction,
)
from trader.recommendation.domain.selection.scored_selection import (
    ScoredCandidateStageCounts,
)


def BuildSupplyStatus(
    projection: ScoredLocalProjection,
    candidate_stage_counts: ScoredCandidateStageCounts | None = None,
    *,
    candidate_quote_eligible: int | None = None,
    freeze: ScoredFreeze | None = None,
    input_stages: tuple[PipelineStageSnapshot, ...] = (),
    scored_stages: ScoredStageChain | None = None,
) -> InputQualityStatus:
    if candidate_stage_counts is None and projection.selection.stage_counts is None:
        raise ValueError("scored input status requires immutable candidate stage facts")
    quality = projection.input_quality
    requested = set(projection.native_input.requested_codes)
    evaluations = tuple(item for item in projection.selection.evaluations if item.code in requested)
    published_freeze = freeze or projection.local
    diagnostics = published_freeze.selection_diagnostics
    if diagnostics is None:
        raise ValueError("scored input status requires selection diagnostics")
    freeze_items = published_freeze.items
    scored_chain = scored_stages or projection.stages
    snapshots = JoinStageSnapshots(input_stages, scored_chain.snapshots) if input_stages else ()
    pipeline = ProjectPipelineFacts(
        snapshots or scored_chain.snapshots,
        freeze=published_freeze,
        quality=quality,
        stage_counts=candidate_stage_counts or projection.selection.stage_counts,
    )
    reasons: Counter[str] = Counter()
    for item in evaluations:
        reasons.update(reason.code for reason in item.filter_reasons)
        reasons.update(reason.code for reason in item.optional_flags)
        if item.candidate_audit_pruning_reason:
            reasons[item.candidate_audit_pruning_reason] += 1
        if item.selection_skip_reason:
            reasons[item.selection_skip_reason] += 1
    reasons.update(item.reason for item in freeze_items if item.reason)
    reasons.update(risk for item in freeze_items for risk in item.risk_codes)
    return InputQualityStatus(
        strategy=projection.local.strategy,
        status=quality.status,
        publishable=quality.publishable,
        summary=_supply_summary(projection, freeze=published_freeze),
        pipeline=pipeline,
        stage_snapshots=snapshots,
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


def JoinStageSnapshots(
    prepared: tuple[PipelineStageSnapshot, ...],
    scored: tuple[PipelineStageSnapshot, ...],
) -> tuple[PipelineStageSnapshot, ...]:
    snapshots = (*prepared, *scored)
    ValidateStageBatchContinuity(snapshots)
    return snapshots


def BuildPendingStageSnapshots(
    prepared: tuple[PipelineStageSnapshot, ...],
) -> tuple[PipelineStageSnapshot, ...]:
    if tuple(item.stage for item in prepared) != PIPELINE_STAGES[: len(prepared)]:
        raise ValueError("input observations must be an ordered pipeline prefix")
    snapshots = list(prepared)
    previous = prepared[-1]
    for stage in PIPELINE_STAGES[len(prepared) :]:
        snapshot = StageOutput(
            stage,
            (),
            input_batch_id=previous.output_batch_id,
            as_of=previous.as_of,
            input_count=previous.output_count,
            pending_count=previous.output_count,
            source_health=previous.source_health,
            latency_ms=None,
        ).snapshot
        snapshots.append(snapshot)
        previous = snapshot
    ValidateStageBatchContinuity(tuple(snapshots))
    return tuple(snapshots)


def ProjectPipelineFacts(
    snapshots: tuple[PipelineStageSnapshot, ...],
    *,
    freeze: ScoredFreeze | None = None,
    quality: ScoredInputQuality | None = None,
    stage_counts: ScoredCandidateStageCounts | None = None,
) -> RecommendationPipelineStatus:
    by_stage = {item.stage: item for item in snapshots}
    diagnostics: list[PipelineStageStatus] = []
    current: PipelineStageKey = "local_data"
    for stage, key in zip(PIPELINE_STAGES, PIPELINE_STAGE_ORDER, strict=True):
        fact = by_stage.get(stage)
        if fact is None:
            diagnostics.append(PipelineStageStatus(key, "pending", None, None))
            continue
        if fact.latency_ms is not None:
            current = key
        facets: tuple[PipelineFacet, ...] = ()
        if stage is PipelineStage.MARKET_FEATURES and quality is not None:
            facets = (
                PipelineFacet("candidate_features", quality.candidate_feature_count),
                PipelineFacet("security_master", quality.security_master_covered_count),
                PipelineFacet("history", quality.history_covered_count),
            )
        if stage is PipelineStage.MARKET_FEATURES and stage_counts is not None:
            facets += (PipelineFacet("strategy_history_eligible", stage_counts.strategy_history_eligible),)
        if stage is PipelineStage.MODEL_FILTER:
            facets = (PipelineFacet("model_input_eligible", fact.input_count - fact.pending_count - fact.failed_count),)
        if stage is PipelineStage.RISK_REVIEW:
            counts = {item.code: item.count for item in fact.reasons}
            facets = tuple(
                PipelineFacet(key, counts.get(key, 0))
                for key in (
                    "deepseek_eligible",
                    "deepseek_submitted",
                    "deepseek_completed",
                    "deepseek_cache_reused",
                    "deepseek_veto",
                    "deepseek_source_failed",
                )
            )
        if stage is PipelineStage.ACTION_PUBLISH and freeze is not None:
            facets = tuple(
                PipelineFacet(
                    f"action_{action.value}", sum(item.action is action for item in freeze.items), len(freeze.items)
                )
                for action in RecommendationAction
            )
        diagnostics.append(
            PipelineStageStatus(
                key,
                "pending" if fact.latency_ms is None else "completed" if fact.state.value == "ready" else "degraded",
                fact.input_count,
                fact.output_count,
                facets=facets,
                reason_counts=tuple(PipelineReasonCount(item.code, item.count) for item in fact.reasons),
                duration_ms=fact.latency_ms,
                rejected_count=fact.rejected_count,
                pending_count=fact.pending_count,
                failed_count=fact.failed_count,
                not_selected_count=fact.not_selected_count,
            )
        )
    complete = snapshots if tuple(item.stage for item in snapshots) == PIPELINE_STAGES else ()
    return RecommendationPipelineStatus(current, tuple(diagnostics), complete)


def UpdateSupplyStatusFreeze(
    current: InputQualityStatus,
    projection: ScoredLocalProjection,
    freeze: ScoredFreeze,
    *,
    scored_stages: ScoredStageChain,
) -> InputQualityStatus:
    scoring_keys = {snapshot.stage for snapshot in scored_stages.snapshots}
    return BuildSupplyStatus(
        projection,
        projection.selection.stage_counts,
        candidate_quote_eligible=current.candidate_feature_count,
        freeze=freeze,
        input_stages=tuple(snapshot for snapshot in current.stage_snapshots if snapshot.stage not in scoring_keys),
        scored_stages=scored_stages,
    )


def _supply_summary(projection: ScoredLocalProjection, *, freeze: ScoredFreeze | None = None) -> SupplySummary:
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
    highest = max((item.final_score for item in (freeze or projection.local).items), default=None)
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
    # A partially degraded upstream population does not explain a completely
    # blocked downstream stage. Prefer the first stage that consumes all output.
    for stage in pipeline.stages:
        if stage.input_count and stage.output_count == 0 and (stage.pending_count or stage.failed_count):
            return stage.reason_counts[0].reason if stage.reason_counts else f"{stage.key}_pending"
    for stage in pipeline.stages:
        if stage.pending_count or stage.failed_count:
            return stage.reason_counts[0].reason if stage.reason_counts else f"{stage.key}_pending"
        if stage.input_count and stage.output_count == 0:
            return stage.reason_counts[0].reason if stage.reason_counts else f"{stage.key}_empty"
    if empty_reason:
        return empty_reason
    actions = pipeline.stage("action_publish")
    if not any(item.key == "action_executable" and item.count for item in actions.facets):
        return "no_executable_candidates"
    return "ready"


__all__ = [
    "BuildPendingStageSnapshots",
    "ProjectPipelineFacts",
    "BuildSupplyStatus",
    "UpdateSupplyStatusFreeze",
]
