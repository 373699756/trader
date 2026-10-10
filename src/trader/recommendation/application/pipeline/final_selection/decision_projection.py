"""Final local and hybrid projection to the unified decision identity."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from trader.recommendation.application.pipeline.downside_action.downside_protection import (
    RiskControlPort,
    RiskControlService,
)
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import (
    ScoredSelectionIdentity,
    ScoredSelectionOptions,
)
from trader.recommendation.application.pipeline.final_selection.grouped_ranking import (
    RankingSelectionPort,
    RankingSelectionService,
)
from trader.recommendation.application.pipeline.final_selection.scored_stage_chain import (
    ScoredStageChain,
    ScoredStageContext,
    complete_scored_stages,
    reason_aggregates,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.pipeline.policy_projection import preselection_replay_feature
from trader.recommendation.application.pipeline.quality_check.input_quality_service import (
    QualityScoringBatch,
    ScoredInputQuality,
    ScoredInputQualityOptions,
    assess_scored_input_quality,
    prepare_native_quality_batch,
)
from trader.recommendation.application.pipeline.risk_review.deepseek_evidence_gate import (
    normalize_scored_review_times,
    scored_decision_policy,
)
from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput, measured_output, stage_output
from trader.recommendation.application.ports.loaded_profile import (
    ModelDiagnostics,
    ModelScoreBatch,
    ModelScoringContext,
    ModelScoringPort,
)
from trader.recommendation.application.ports.scoring import ScoredNativeInput
from trader.recommendation.domain.candidate.composition import WEIGHTED_EVIDENCE_SCORE_SCALE
from trader.recommendation.domain.candidate.filters import hard_filter
from trader.recommendation.domain.evidence.pipeline import PipelineStage
from trader.recommendation.domain.evidence.review import DeepSeekReview, ReviewOutcome
from trader.recommendation.domain.market.models import FeatureSnapshot, MarketQuote
from trader.recommendation.domain.publication.decision_identity import (
    DecisionDownside,
    DecisionItem,
    DecisionModelDiagnostics,
    DecisionQuote,
    DecisionResearchCoverage,
    ScoredDecision,
    SelectionDiagnostics,
)
from trader.recommendation.domain.publication.models import (
    RecommendationAction,
    ScoredSelectionResult,
    ScoredStockEvaluation,
    Strategy,
)
from trader.recommendation.domain.risk.downside import DownsideAssessment
from trader.recommendation.domain.risk.scored_fusion import (
    DecisionEpoch,
    ScoredDecisionEntry,
    ScoredDecisionPolicy,
    ScoredDecisionRequest,
    ScoredReviewCandidate,
    build_scored_decision_epoch,
    select_scored_review_candidates,
)
from trader.recommendation.domain.selection.scored_selection import ScoredCandidateStageCounts


@dataclass(frozen=True)
class ScoredLocalProjection:
    native_input: ScoredNativeInput
    selection: ScoredSelectionResult
    input_quality: ScoredInputQuality
    review_candidates: tuple[ScoredReviewCandidate, ...]
    local_epoch: DecisionEpoch
    local: ScoredDecision
    stages: ScoredStageChain
    score_model_version: str | None = None
    model_diagnostics: tuple[tuple[str, ModelDiagnostics], ...] = ()
    downside_assessments: tuple[tuple[str, DownsideAssessment], ...] = ()


@dataclass(frozen=True)
class ScoredReviewProjection:
    decision: ScoredDecision | None
    stages: ScoredStageChain


@dataclass(frozen=True)
class _DecisionProjectionContext:
    input_version: str
    strategy: Strategy
    decision_policy: ScoredDecisionPolicy
    downside_assessments: tuple[tuple[str, DownsideAssessment], ...]
    parent_version: str | None = None
    score_model_version: str | None = None
    model_diagnostics: Mapping[str, ModelDiagnostics] | None = None


@dataclass(frozen=True)
class ScoredProjectionInputs:
    model_scoring: ModelScoringPort | None = None
    scoring_context: ModelScoringContext | None = None
    candidate_stage_counts: ScoredCandidateStageCounts | None = None
    preselection_transient_invalid: bool = False
    risk_control: RiskControlPort | None = None
    ranking_selection: RankingSelectionPort | None = None
    quality_batch: QualityScoringBatch | None = None
    monotonic: Callable[[], float] = field(default=time.monotonic, kw_only=True)


def build_scored_local(
    native_input: ScoredNativeInput,
    policy: RecommendationPolicy,
    *,
    sequence: int,
    runtime: ScoredProjectionInputs | None = None,
) -> ScoredLocalProjection:
    if sequence < 1:
        raise ValueError("scored decision sequence must be positive")
    runtime = runtime if runtime is not None else ScoredProjectionInputs()
    if runtime.quality_batch is None:
        minimum_history = (
            runtime.model_scoring.history_required_sessions(native_input.strategy)
            if runtime.model_scoring is not None
            and native_input.phase != "close_fallback"
            and runtime.model_scoring.uses_model(native_input.strategy)
            else 20
        )
        quality_batch = prepare_native_quality_batch(
            native_input, policy.hard_filter, minimum_history, runtime.monotonic
        )
    else:
        quality_batch = runtime.quality_batch
        if quality_batch.native_input != native_input:
            raise ValueError("scoring context does not match its quality batch")
    native_input = quality_batch.native_input
    scoring_input = replace(native_input, candidate_features=quality_batch.output.records)
    started = runtime.monotonic()
    strategy = native_input.strategy
    decision_policy = scored_decision_policy(policy, strategy, phase=native_input.phase)
    risk_control = runtime.risk_control if runtime.risk_control is not None else RiskControlService()
    ranking_selection = (
        runtime.ranking_selection if runtime.ranking_selection is not None else RankingSelectionService()
    )
    population = tuple(preselection_replay_feature(feature) for feature in native_input.market_features)
    model_scoring = runtime.model_scoring
    uses_model = (
        model_scoring is not None and native_input.phase != "close_fallback" and model_scoring.uses_model(strategy)
    )
    model_batch = (
        model_scoring.score(
            strategy,
            _model_eligible_candidates(scoring_input, policy),
            context=runtime.scoring_context,
        )
        if model_scoring is not None and uses_model
        else None
    )
    minimum_history_sessions = (
        model_scoring.history_required_sessions(strategy) if model_scoring is not None and uses_model else 20
    )
    profile_history_qualified_codes = (
        frozenset(
            feature.quote.code
            for feature in quality_batch.output.records
            if model_scoring is not None and model_scoring.is_input_eligible(strategy, feature)
        )
        if model_scoring is not None and uses_model
        else None
    )
    selection = ranking_selection.select(
        population,
        policy,
        ScoredSelectionOptions(
            evaluated_at=native_input.evaluated_at,
            max_age_seconds=native_input.score_max_age_seconds,
            population_evaluated_at=_market_population_watermark(native_input.market_features),
            population_max_age_seconds=native_input.preselect_max_age_seconds,
            phase=native_input.phase,
            # The quality batch also retains directed inputs for rejected/pending
            # audit records; they cannot become a scoring output (checked below).
            candidate_features=quality_batch.native_input.candidate_features,
            normalize_discovery_source_time=True,
            strategy=strategy,
            minimum_history_sessions=minimum_history_sessions,
            model_input_eligible_codes=profile_history_qualified_codes,
            candidate_limit_per_board=native_input.candidate_pool_size,
        ),
        ScoredSelectionIdentity(
            trade_date=native_input.trade_date,
            data_version=native_input.data_version,
            merge_epoch=native_input.input_version,
        ),
        execution_gate_reasons=_model_execution_gate_reasons(model_batch, strategy),
    )
    quality = assess_scored_input_quality(
        native_input,
        selection,
        ScoredInputQualityOptions(
            minimum_history_sessions=minimum_history_sessions,
            profile_history_qualified_codes=profile_history_qualified_codes,
            candidate_stage_counts=runtime.candidate_stage_counts,
            preselection_transient_invalid=runtime.preselection_transient_invalid,
        ),
    )
    candidates = select_scored_review_candidates(selection, decision_policy)
    scored_stage = _local_score_stage(
        quality_batch.output, selection, runtime.monotonic, started, f"{native_input.input_version}:{sequence}"
    )
    input_version = native_input.input_version
    decision_request = ScoredDecisionRequest(
        selection=selection,
        reviews={},
        observed_at=native_input.evaluated_at,
        trade_date=native_input.trade_date,
        sequence=sequence,
        config_version=native_input.config_version,
        strategy_version=policy.strategy_version,
        fusion_version=policy.fusion_version,
        market_epoch_version=f"native-market:{input_version}",
        candidate_epoch_version=(f"native-candidate:{input_version}" if native_input.candidate_features else None),
        research_epoch_version=None,
        projection_stage="local",
        parent_decision_version=None,
        review_candidate_codes=tuple(item.code for item in candidates),
        degraded_reasons=quality.degraded_reasons,
        policy=decision_policy,
    )
    stages = complete_scored_stages(
        scored_stage,
        ScoredStageContext(
            decision_policy, native_input.evaluated_at, decision_request.review_candidate_codes, (), f"local:{sequence}"
        ),
        runtime.monotonic,
    )
    epoch = build_scored_decision_epoch(decision_request, entries=stages.entries)
    model_version = model_batch.model_version if model_batch is not None else None
    downside_assessments = tuple((item.code, risk_control.assess(item.features, strategy)) for item in epoch.entries)
    return ScoredLocalProjection(
        native_input=native_input,
        stages=stages,
        selection=selection,
        input_quality=quality,
        review_candidates=candidates,
        local_epoch=epoch,
        local=_scored_decision(
            epoch,
            _DecisionProjectionContext(
                input_version=native_input.input_version,
                strategy=strategy,
                decision_policy=decision_policy,
                downside_assessments=downside_assessments,
                score_model_version=model_version,
                model_diagnostics=model_batch.diagnostics if model_batch is not None else None,
            ),
        ),
        score_model_version=model_version,
        model_diagnostics=tuple(sorted(model_batch.diagnostics.items())) if model_batch is not None else (),
        downside_assessments=downside_assessments,
    )


def _local_score_stage(
    source: PipelineStageOutput[FeatureSnapshot],
    selection: ScoredSelectionResult,
    monotonic: Callable[[], float],
    started: float,
    scoring_identity: str,
) -> PipelineStageOutput[ScoredStockEvaluation]:
    scored = selection.scored_candidates
    accepted = frozenset(item.code for item in scored)
    quality_codes = frozenset(feature.quote.code for feature in source.records)
    reasons = Counter(
        item.selection_skip_reason or item.candidate_audit_pruning_reason or "not_scored"
        for item in selection.evaluations
        if item.code in quality_codes and item.code not in accepted
    )
    if not accepted <= quality_codes:
        raise ValueError("local scoring output exceeds its quality input")
    return measured_output(
        stage_output(
            PipelineStage.LOCAL_SCORE,
            scored,
            input_batch_id=source.snapshot.output_batch_id,
            output_batch_id=f"{source.snapshot.output_batch_id}:local_score:{scoring_identity}",
            as_of=source.snapshot.as_of,
            input_count=len(source.records),
            reasons=reason_aggregates(reasons),
            pending_count=sum(
                count for reason, count in reasons.items() if reason == "production_model_features_missing"
            ),
            source_health=source.snapshot.source_health,
            latency_ms=0,
        ),
        started,
        monotonic,
    )


def _model_execution_gate_reasons(
    model_batch: ModelScoreBatch | None,
    strategy: Strategy,
) -> Mapping[str, str]:
    if model_batch is None:
        return {}
    if strategy not in {Strategy.TOMORROW, Strategy.D25}:
        return {}
    return {
        code: "model_net_utility_non_positive"
        for code, diagnostics in model_batch.diagnostics.items()
        if diagnostics.predicted_net_excess_pct <= 0.0
    }


def _model_eligible_candidates(
    native_input: ScoredNativeInput,
    policy: RecommendationPolicy,
) -> tuple[FeatureSnapshot, ...]:
    eligible: list[FeatureSnapshot] = []
    for feature in native_input.candidate_features:
        filtered = hard_filter(
            feature,
            native_input.evaluated_at,
            max_age_seconds=native_input.score_max_age_seconds,
            policy=policy.hard_filter,
        )
        if filtered.allowed:
            eligible.append(replace(feature, quote=replace(feature.quote, board=filtered.board)))
    return tuple(eligible)


def _market_population_watermark(features: tuple[FeatureSnapshot, ...]) -> datetime:
    return max(
        value
        for feature in features
        for value in (feature.observed_at, feature.quote.source_time, feature.quote.received_time)
    )


def build_scored_hybrid(
    projection: ScoredLocalProjection,
    policy: RecommendationPolicy,
    reviews: Mapping[str, DeepSeekReview],
    *,
    review_deadline: datetime,
    monotonic: Callable[[], float] = time.monotonic,
    review_latency_ms: int = 0,
) -> ScoredReviewProjection | None:
    strategy = projection.local.strategy
    if projection.native_input.strategy is not strategy:
        return None
    decision_policy = scored_decision_policy(policy, strategy, phase=projection.native_input.phase)
    candidates = {item.code for item in projection.review_candidates}
    if any(code not in candidates or review.code != code for code, review in reviews.items()):
        return None
    normalized = normalize_scored_review_times(reviews, review_deadline)
    if normalized is None:
        return None
    usable = {
        code: review
        for code, review in normalized.items()
        if review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN} and review.completed_at < review_deadline
    }
    observed_at = max(
        (
            projection.native_input.evaluated_at,
            *(review.completed_at for review in normalized.values()),
        )
    )
    stages = complete_scored_stages(
        projection.stages.local_score,
        ScoredStageContext(
            decision_policy,
            observed_at,
            tuple(item.code for item in projection.review_candidates),
            tuple(normalized.values()),
            f"review:{projection.local.sequence + 1}",
        ),
        monotonic,
        review_latency_ms=review_latency_ms,
        review_attempted=True,
    )
    if not usable:
        return ScoredReviewProjection(None, stages)
    decision_request = ScoredDecisionRequest(
        selection=projection.selection,
        reviews=normalized,
        observed_at=observed_at,
        trade_date=projection.native_input.trade_date,
        sequence=projection.local.sequence + 1,
        config_version=projection.native_input.config_version,
        strategy_version=policy.strategy_version,
        fusion_version=policy.fusion_version,
        market_epoch_version=projection.local_epoch.market_epoch_version,
        candidate_epoch_version=projection.local_epoch.candidate_epoch_version,
        research_epoch_version=None,
        projection_stage="hybrid",
        parent_decision_version=projection.local_epoch.version,
        review_candidate_codes=tuple(item.code for item in projection.review_candidates),
        degraded_reasons=tuple(
            sorted(
                {
                    *projection.input_quality.degraded_reasons,
                    *(() if set(usable) == candidates else ("deepseek_incomplete",)),
                }
            )
        ),
        policy=decision_policy,
    )
    epoch = build_scored_decision_epoch(decision_request, entries=stages.entries)
    decision = _scored_decision(
        epoch,
        _DecisionProjectionContext(
            input_version=projection.native_input.input_version,
            downside_assessments=projection.downside_assessments,
            parent_version=projection.local.version,
            strategy=strategy,
            decision_policy=decision_policy,
            score_model_version=projection.score_model_version,
            model_diagnostics=dict(projection.model_diagnostics),
        ),
    )
    return ScoredReviewProjection(decision, stages)


def _scored_decision(
    epoch: DecisionEpoch,
    context: _DecisionProjectionContext,
) -> ScoredDecision:
    downside_assessments = dict(context.downside_assessments)
    return ScoredDecision(
        strategy=context.strategy,
        trade_date=epoch.trade_date,
        sequence=epoch.sequence,
        observed_at=epoch.observed_at,
        stage=epoch.projection_stage,
        parent_version=context.parent_version,
        input_versions=tuple(
            (name, value)
            for name, value in (
                ("native", context.input_version),
                ("market", epoch.market_epoch_version),
                ("candidate", epoch.candidate_epoch_version),
                ("research", epoch.research_epoch_version),
                ("score_scale", WEIGHTED_EVIDENCE_SCORE_SCALE),
                ("score_model", context.score_model_version),
            )
            if value is not None
        ),
        config_version=epoch.config_version,
        strategy_version=epoch.strategy_version,
        fusion_version=epoch.fusion_version,
        items=tuple(
            _decision_item(
                item,
                strategy=context.strategy,
                review_eligible=item.code in epoch.review_candidate_codes,
                model_diagnostics=(context.model_diagnostics or {}).get(item.code),
                downside=downside_assessments[item.code],
            )
            for item in epoch.entries
        ),
        filter_aggregates=tuple(epoch.filter_reason_counts.items()),
        degraded_reasons=epoch.degraded_reasons,
        population_count=epoch.evaluated_count,
        rejected_count=epoch.rejected_count,
        selection_diagnostics=_selection_diagnostics(epoch, context),
    )


def _decision_item(
    entry: ScoredDecisionEntry,
    *,
    strategy: Strategy,
    review_eligible: bool,
    model_diagnostics: ModelDiagnostics | None,
    downside: DownsideAssessment,
) -> DecisionItem:
    reason = entry.decision_skip_reason or entry.action_reason or "not_selected"
    return DecisionItem(
        code=entry.code,
        action=entry.action,
        selected=entry.selected,
        rank=entry.rank,
        candidate_score=entry.candidate_score,
        local_score=entry.score.local_score,
        final_score=entry.score.final_score,
        score_components=(
            *tuple(entry.score.components.items()),
            ("deepseek_score", entry.score.deepseek_score),
            ("deepseek_risk_penalty", entry.score.deepseek_risk_penalty),
        ),
        risk_codes=tuple(fact.risk_code for fact in (*entry.local_risk_facts, *entry.deepseek_risk_facts)),
        reason=reason,
        board=entry.features.quote.board,
        selection_rank=entry.selection_rank,
        name=entry.features.quote.name,
        industry=entry.features.quote.industry,
        quote=_decision_quote(entry.features.quote),
        setup_type=downside.setup_type,
        downside=DecisionDownside(
            downside.status,
            downside.reasons,
            downside.atr20_pct,
            downside.intraday_reversal_atr,
            downside.historical_drawdown_pct,
        ),
        review_outcome=entry.review_outcome.value if entry.review_outcome is not None else None,
        research_coverage=DecisionResearchCoverage(
            len(entry.features.evidence),
            len(entry.features.external_risk_facts),
            review_eligible,
        ),
        model_diagnostics=(
            DecisionModelDiagnostics(
                signal_score=model_diagnostics.signal_score,
                predicted_excess_return_pct=model_diagnostics.predicted_excess_return_pct,
                estimated_cost_pct=model_diagnostics.estimated_cost_pct,
                predicted_net_excess_pct=model_diagnostics.predicted_net_excess_pct,
                model_disagreement_pct=model_diagnostics.model_disagreement_pct,
            )
            if model_diagnostics is not None
            else None
        ),
    )


def _selection_diagnostics(
    epoch: DecisionEpoch,
    context: _DecisionProjectionContext,
) -> SelectionDiagnostics:
    policy = context.decision_policy
    selected = tuple(item for item in epoch.entries if item.selected)
    maximum_final_score = max((item.score.final_score for item in epoch.entries), default=None)
    observation_floor = max(0.0, policy.executable_threshold - policy.observation_margin)
    empty_reason = None
    if not epoch.entries:
        empty_reason = "no_scored_candidates"
    elif not selected:
        if _has_no_positive_model_utility(epoch, context):
            empty_reason = "no_positive_net_utility"
        elif maximum_final_score is not None and maximum_final_score < observation_floor:
            empty_reason = "score_below_observation_floor"
        else:
            empty_reason = "risk_or_execution_blocked"
    return SelectionDiagnostics(
        maximum_final_score=maximum_final_score,
        executable_threshold=policy.executable_threshold,
        observation_floor=observation_floor,
        executable_limit=policy.top_k,
        observation_limit=policy.observation_limit,
        selected_executable_count=sum(item.action is RecommendationAction.EXECUTABLE for item in selected),
        selected_observation_count=sum(item.action is RecommendationAction.OBSERVE for item in selected),
        review_candidate_count=len(epoch.review_candidate_codes),
        empty_reason=empty_reason,
        evaluated_count=len(epoch.entries),
    )


def _has_no_positive_model_utility(
    epoch: DecisionEpoch,
    context: _DecisionProjectionContext,
) -> bool:
    return (
        context.strategy is Strategy.TOMORROW
        and context.model_diagnostics is not None
        and bool(epoch.entries)
        and bool(context.model_diagnostics)
        and all(item.predicted_net_excess_pct <= 0.0 for item in context.model_diagnostics.values())
    )


def _decision_quote(quote: MarketQuote) -> DecisionQuote:
    if quote.price is None:
        raise ValueError("selected decision quote price is unavailable")
    return DecisionQuote(
        code=quote.code,
        price=quote.price,
        pct_change=quote.pct_change,
        amount=quote.amount,
        turnover_rate=quote.turnover_rate,
        market_cap=quote.market_cap,
        source=quote.source,
        source_time=quote.source_time,
        data_version=quote.data_version,
    )


def validate_review_manifests(
    projection: ScoredLocalProjection,
    reviews: Mapping[str, DeepSeekReview],
    expected: Mapping[str, str],
) -> bool:
    candidate_codes = {item.code for item in projection.review_candidates}
    return not any(
        code not in candidate_codes
        or review.code != code
        or (
            review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN}
            and review.evidence_manifest_hash != expected.get(code)
        )
        for code, review in reviews.items()
    )


__all__ = [
    "ScoredLocalProjection",
    "build_scored_hybrid",
    "build_scored_local",
    "validate_review_manifests",
]
