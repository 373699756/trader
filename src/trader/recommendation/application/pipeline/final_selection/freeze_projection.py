"""Final local and hybrid projection to the unified freeze identity."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import time as wall_time
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
    CompleteReviewStages,
    FixScoredSeats,
    ReasonAggregates,
    ScoredStageChain,
    ScoredStageContext,
    ScoredReviewStages,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.pipeline.quality_check.input_quality_service import (
    AssessScoredInputQuality,
    PrepareNativeQualityBatch,
    QualityScoringBatch,
    ScoredInputQuality,
    ScoredInputQualityOptions,
)
from trader.recommendation.application.pipeline.risk_review.deepseek_evidence_gate import (
    BuildScoredFreezePolicy,
    NormalizeScoredReviewTimes,
)
from trader.recommendation.application.pipeline.stage_output import MeasuredOutput, PipelineStageOutput, StageOutput
from trader.recommendation.application.ports.loaded_profile import (
    ModelDiagnostics,
    TomorrowProbability,
    ModelScoreBatch,
    ModelScoringContext,
    ModelScoringPort,
)
from trader.recommendation.application.ports.scoring import ScoredNativeInput
from trader.recommendation.domain.candidate.composition import WEIGHTED_EVIDENCE_SCORE_SCALE
from trader.recommendation.domain.candidate.filters import ApplyFilters, DefaultFilterRules
from trader.recommendation.domain.evidence.pipeline import PipelineStage, PipelineStageSnapshot
from trader.recommendation.domain.evidence.review import DeepSeekReview, ReviewOutcome
from trader.recommendation.domain.market.history_alignment import HISTORY_INPUT_FAILURES
from trader.recommendation.domain.market.models import FeatureSnapshot, MarketQuote
from trader.recommendation.domain.market.tomorrow_probability import IsTomorrowClosingQuote
from trader.recommendation.domain.outcomes.prediction_target import ParsePredictionTargetIdentity, PredictionTarget
from trader.recommendation.domain.outcomes.tomorrow_target import TOMORROW_CLOSE_GAIN_TARGET
from trader.recommendation.domain.publication.freeze_identity import (
    FreezeDownside,
    FreezeItem,
    FreezeModelDiagnostics,
    FreezeQuote,
    FreezeResearchCoverage,
    FreezeRiskDisclosure,
    ScoredFreeze,
    SelectionDiagnostics,
)
from trader.recommendation.domain.publication.models import (
    RecommendationAction,
    ScoredSelectionResult,
    ScoredStockEvaluation,
    Strategy,
)
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.domain.risk.downside import DownsideAssessment
from trader.recommendation.domain.risk.scored_fusion import (
    BuildScoredFreezeEpoch,
    FreezeEpoch,
    FreezeSelectionLimits,
    ScoredFreezeEntry,
    ScoredFreezePolicy,
    ScoredFreezeRequest,
    ScoredReviewCandidate,
    SelectScoredReviewCandidates,
    ValidateScoredFreezeReviews,
)
from trader.recommendation.domain.scoring.profile_identity import MODEL_SELECTION_SCORE_SCALES, TOMORROW_PROBABILITY_SCORE_SCALE
from trader.recommendation.domain.selection.scored_selection import (
    ApplySelectionRisks,
    FilteredCandidateInputs,
    PreselectionReplayFeature,
    QualifiedPopulationFeatures,
    ValidatePopulationQualifications,
    LocalRiskPolicy,
    ScoredCandidateStageCounts,
)


@dataclass(frozen=True)
class ScoredLocalProjection:
    native_input: ScoredNativeInput
    selection: ScoredSelectionResult
    input_quality: ScoredInputQuality
    review_candidates: tuple[ScoredReviewCandidate, ...]
    local_epoch: FreezeEpoch
    local: ScoredFreeze
    stages: ScoredStageChain
    score_model_version: str | None = None
    model_diagnostics: tuple[tuple[str, ModelDiagnostics | TomorrowProbability], ...] = ()
    downside_assessments: tuple[tuple[str, DownsideAssessment], ...] = ()
    cost_reference_version: str | None = None
    started_monotonic: float | None = None
    score_scale: str = WEIGHTED_EVIDENCE_SCORE_SCALE
    score_model_id: str | None = None
    score_contract_hash: str | None = None
    production_authority: bool = True
    rank_population_version: str | None = None


@dataclass(frozen=True)
class ScoredReviewDraft:
    """The fixed seats and summaries needed after local scoring has returned."""

    local: ScoredFreeze
    local_epoch: FreezeEpoch
    native_strategy: Strategy
    phase: str
    evaluated_at: datetime
    input_version: str
    requested_codes: tuple[str, ...]
    prior_snapshots: tuple[PipelineStageSnapshot, ...]
    fixed_selection: PipelineStageOutput[ScoredStockEvaluation]
    review_candidates: tuple[ScoredReviewCandidate, ...]
    input_quality: ScoredInputQuality
    stage_counts: ScoredCandidateStageCounts | None
    started_monotonic: float | None
    model_diagnostics: tuple[tuple[str, ModelDiagnostics | TomorrowProbability], ...]
    no_positive_model_utility: bool
    downside_assessments: tuple[tuple[str, DownsideAssessment], ...]
    score_model_version: str | None
    cost_reference_version: str | None
    score_scale: str
    score_model_id: str | None
    score_contract_hash: str | None
    production_authority: bool
    rank_population_version: str | None


def PrepareScoredReviewDraft(projection: ScoredLocalProjection) -> ScoredReviewDraft:
    source = projection.native_input
    fixed = projection.stages.fixed_selection
    seat_codes = frozenset(item.code for item in fixed.records)
    return ScoredReviewDraft(
        local=projection.local,
        local_epoch=projection.local_epoch,
        native_strategy=source.strategy,
        phase=source.phase,
        evaluated_at=source.evaluated_at,
        input_version=source.input_version,
        requested_codes=source.requested_codes,
        prior_snapshots=projection.stages.snapshots[:3],
        fixed_selection=fixed,
        review_candidates=projection.review_candidates,
        input_quality=projection.input_quality,
        stage_counts=projection.selection.stage_counts,
        started_monotonic=projection.started_monotonic,
        model_diagnostics=tuple(pair for pair in projection.model_diagnostics if pair[0] in seat_codes),
        no_positive_model_utility=_no_positive_model_utility(item for _, item in projection.model_diagnostics),
        downside_assessments=projection.downside_assessments,
        score_model_version=projection.score_model_version,
        cost_reference_version=projection.cost_reference_version,
        score_scale=projection.score_scale,
        score_model_id=projection.score_model_id,
        score_contract_hash=projection.score_contract_hash,
        production_authority=projection.production_authority,
        rank_population_version=projection.rank_population_version,
    )


@dataclass(frozen=True)
class ScoredReviewProjection:
    freeze: ScoredFreeze | None
    stages: ScoredReviewStages


@dataclass(frozen=True)
class _FreezeProjectionContext:
    input_version: str
    strategy: Strategy
    freeze_policy: ScoredFreezePolicy
    downside_assessments: tuple[tuple[str, DownsideAssessment], ...]
    parent_version: str | None = None
    score_model_version: str | None = None
    model_diagnostics: Mapping[str, ModelDiagnostics | TomorrowProbability] | None = None
    no_positive_model_utility: bool = False
    cost_reference_version: str | None = None
    score_scale: str = WEIGHTED_EVIDENCE_SCORE_SCALE
    score_model_id: str | None = None
    score_contract_hash: str | None = None
    production_authority: bool = True
    rank_population_version: str | None = None
    preview_context: PreviewContext | None = None
    official_close_evidence: str | None = None
    prediction_target: PredictionTarget | None = None
    scoring_profile: str | None = None


@dataclass(frozen=True)
class ScoredProjectionInputs:
    model_scoring: ModelScoringPort | None = None
    scoring_context: ModelScoringContext | None = None
    candidate_stage_counts: ScoredCandidateStageCounts | None = None
    preselection_transient_invalid: bool = False
    risk_control: RiskControlPort | None = None
    ranking_selection: RankingSelectionPort | None = None
    quality_batch: QualityScoringBatch | None = None
    population_qualifications: FilteredCandidateInputs | None = None
    monotonic: Callable[[], float] = field(default=time.monotonic, kw_only=True)


def BuildScoredLocal(
    native_input: ScoredNativeInput,
    policy: RecommendationPolicy,
    *,
    sequence: int,
    runtime: ScoredProjectionInputs | None = None,
) -> ScoredLocalProjection:
    if sequence < 1:
        raise ValueError("scored freeze sequence must be positive")
    runtime = runtime if runtime is not None else ScoredProjectionInputs()
    model_scoring = runtime.model_scoring
    profile_status = model_scoring.status() if model_scoring is not None else None
    head_status = profile_status.heads.get(native_input.strategy) if profile_status is not None else None
    model_rank = head_status is not None and head_status.score_scale in MODEL_SELECTION_SCORE_SCALES
    predicts_close_gain = head_status is not None and head_status.score_scale == TOMORROW_PROBABILITY_SCORE_SCALE
    target = head_status.prediction_target if head_status is not None else None
    requires_close_event = predicts_close_gain or target is not None
    if runtime.quality_batch is None:
        minimum_history = (
            model_scoring.history_required_sessions(native_input.strategy)
            if model_scoring is not None
            and (native_input.phase != "close_fallback" or model_rank or requires_close_event)
            and model_scoring.uses_model(native_input.strategy)
            else 20
        )
        quality_batch = PrepareNativeQualityBatch(native_input, policy.hard_filter, minimum_history, runtime.monotonic)
    else:
        quality_batch = runtime.quality_batch
        if quality_batch.native_input != native_input:
            raise ValueError("scoring context does not match its quality batch")
    native_input = quality_batch.native_input
    official_close_evidence = (
        _official_close_evidence(native_input, quality_batch.output.records)
        if requires_close_event else None
    )
    if requires_close_event and native_input.phase == "close_fallback" and official_close_evidence is None:
        raise ValueError(f"{native_input.strategy.value}_closing_evidence_pending")
    population_qualifications = runtime.population_qualifications
    if population_qualifications is not None:
        ValidatePopulationQualifications(
            population_qualifications,
            native_input.market_features,
            evaluated_at=native_input.evaluated_at,
            hard_filter=policy.hard_filter,
            strategy=native_input.strategy,
            max_age_seconds=native_input.preselect_max_age_seconds,
        )
    scoring_input = replace(native_input, candidate_features=quality_batch.output.records)
    started = runtime.monotonic()
    strategy = native_input.strategy
    freeze_policy = BuildScoredFreezePolicy(policy, strategy, phase=native_input.phase)
    risk_control = runtime.risk_control if runtime.risk_control is not None else RiskControlService()
    ranking_selection = (
        runtime.ranking_selection if runtime.ranking_selection is not None else RankingSelectionService(monotonic=runtime.monotonic)
    )
    population = native_input.market_features
    if population_qualifications is None:
        population = tuple(PreselectionReplayFeature(feature, hard_filter=policy.hard_filter) for feature in population)
    uses_model = (
        model_scoring is not None
        and (native_input.phase != "close_fallback" or model_rank or requires_close_event)
        and model_scoring.uses_model(strategy)
    )
    prediction_inputs = (
        _model_eligible_features(
            scoring_input.candidate_features,
            policy,
            evaluated_at=native_input.evaluated_at,
            max_age_seconds=native_input.score_max_age_seconds,
        )
        if uses_model else ()
    )
    model_batch = (
        model_scoring.score(
            strategy,
            prediction_inputs,
            context=replace(
                runtime.scoring_context or ModelScoringContext(),
                population_features=(
                    QualifiedPopulationFeatures(population_qualifications)
                    if population_qualifications is not None
                    else _model_eligible_features(
                        native_input.market_features,
                        policy,
                        evaluated_at=(
                            _market_population_watermark(native_input.market_features)
                            if native_input.market_features
                            else native_input.evaluated_at
                        ),
                        max_age_seconds=native_input.preselect_max_age_seconds,
                    )
                ),
                population_identity=(
                    f"{native_input.trade_date.isoformat()}:{native_input.phase}:{native_input.config_version}"
                ),
            ),
        )
        if model_scoring is not None and uses_model
        else None
    )
    minimum_history_sessions = (
        model_scoring.history_required_sessions(strategy) if model_scoring is not None and uses_model else 20
    )
    production_authority = (
        head_status.production_authority
        if predicts_close_gain and head_status is not None
        else profile_status is None or profile_status.production_authority
    ) and (
        model_batch is None or model_batch.production_authority
    )
    if model_rank and (model_batch is None or model_batch.score_scale not in MODEL_SELECTION_SCORE_SCALES):
        raise ValueError("model_rank_unavailable")
    if predicts_close_gain and prediction_inputs and model_batch is not None and not model_batch.diagnostics:
        raise ValueError("tomorrow_probability_inputs_pending")
    if target is not None and (model_batch is None or model_batch.prediction_target != target):
        raise ValueError(f"{strategy.value}_prediction_target_unavailable")
    profile_history_qualified_codes = (
        frozenset(
            feature.quote.code
            for feature in quality_batch.output.records
            if model_scoring is not None and model_scoring.is_input_eligible(strategy, feature)
        )
        if model_scoring is not None and uses_model
        else None
    )
    model_reasons = _model_execution_gate_reasons(model_batch, strategy)
    quality_codes = frozenset(item.quote.code for item in quality_batch.output.records)
    missing_model_codes = frozenset(
        feature.quote.code
        for feature in quality_batch.output.records
        if uses_model and (model_batch is None or feature.quote.code not in model_batch.diagnostics)
    )
    model_records = tuple(
        feature
        for feature in quality_batch.output.records
        if feature.quote.code not in model_reasons and feature.quote.code not in missing_model_codes
    )
    missing_model_reasons = {
        feature.quote.code: (
            feature.model_history_quality.failure
            if feature.model_history_quality is not None and feature.model_history_quality.failure
            else "production_model_features_missing"
        )
        for feature in quality_batch.output.records
        if feature.quote.code in missing_model_codes
    }
    model_stage = MeasuredOutput(
        StageOutput(
            PipelineStage.MODEL_FILTER,
            model_records,
            input_batch_id=quality_batch.output.snapshot.output_batch_id,
            as_of=native_input.evaluated_at,
            input_count=len(quality_batch.output.records),
            rejected_count=len(model_reasons),
            pending_count=len(missing_model_codes),
            reasons=ReasonAggregates(Counter(model_reasons.values()) + Counter(missing_model_reasons.values())),
            source_health=quality_batch.output.snapshot.source_health,
            latency_ms=0,
        ),
        started,
        runtime.monotonic,
    )
    started = runtime.monotonic()
    ranking_result = ranking_selection.select(
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
            strategy=strategy,
            minimum_history_sessions=minimum_history_sessions,
            model_input_eligible_codes=profile_history_qualified_codes,
            require_profile_history_window=not predicts_close_gain,
        ),
        ScoredSelectionIdentity(
            trade_date=native_input.trade_date,
            data_version=native_input.data_version,
            merge_epoch=native_input.input_version,
        ),
        execution_gate_reasons={
            **{
                feature.quote.code: "input_quality_pending"
                for feature in native_input.candidate_features
                if feature.quote.code not in quality_codes
            },
            **model_reasons,
            **missing_model_reasons,
        },
        population_qualifications=population_qualifications,
    )
    selection = ranking_result.selection
    scored_stage = _local_score_stage(
        model_stage, selection, runtime.monotonic, started, f"{native_input.input_version}:{sequence}",
        operation_durations=ranking_result.operation_durations,
    )
    started = runtime.monotonic()
    selection = ApplySelectionRisks(
        selection,
        LocalRiskPolicy(policy.risk_rules, policy.fusion.local_risk_cap, policy.company_scoring),
        native_input.evaluated_at,
        strategy,
    )
    if model_rank or not production_authority:
        ranked = {
            item.code: replace(
                item,
                ranking_score=(
                    model_batch.diagnostics[item.code].signal_score
                    if model_rank and model_batch is not None
                    else item.ranking_score
                ),
                shadow_only=not production_authority,
                execution_gate_reason=(
                    "local_risk_veto"
                    if predicts_close_gain and any(fact.veto for fact in item.local_risk_facts)
                    else item.execution_gate_reason
                ),
            )
            for item in selection.scored_candidates
        }
        selection = replace(
            selection,
            evaluations=tuple(ranked.get(item.code, item) for item in selection.evaluations),
            scored_candidates=tuple(ranked[item.code] for item in selection.scored_candidates),
            observations=tuple(ranked[item.code] for item in selection.observations),
        )
    risk_stage = MeasuredOutput(
        StageOutput(
            PipelineStage.LOCAL_RISK,
            selection.scored_candidates,
            input_batch_id=scored_stage.snapshot.output_batch_id,
            as_of=native_input.evaluated_at,
            input_count=len(scored_stage.records),
            source_health=scored_stage.snapshot.source_health,
            latency_ms=0,
        ),
        started,
        runtime.monotonic,
    )
    quality = AssessScoredInputQuality(
        native_input,
        selection,
        ScoredInputQualityOptions(
            minimum_history_sessions=minimum_history_sessions,
            profile_history_qualified_codes=profile_history_qualified_codes,
            candidate_stage_counts=runtime.candidate_stage_counts,
            preselection_transient_invalid=runtime.preselection_transient_invalid,
        ),
    )
    fixed = FixScoredSeats(risk_stage, freeze_policy, native_input.evaluated_at, runtime.monotonic)
    candidates = SelectScoredReviewCandidates(fixed.records, freeze_policy) if production_authority else ()
    input_version = native_input.input_version
    freeze_request = ScoredFreezeRequest(
        selection=selection,
        reviews={},
        observed_at=native_input.evaluated_at,
        trade_date=native_input.trade_date,
        sequence=sequence,
        config_version=native_input.config_version,
        strategy_version=policy.strategy_version,

        market_epoch_version=f"native-market:{input_version}",
        candidate_epoch_version=(f"native-candidate:{input_version}" if native_input.candidate_features else None),
        research_epoch_version=None,
        projection_stage="local",
        parent_freeze_version=None,
        review_candidate_codes=tuple(item.code for item in candidates),
        degraded_reasons=quality.degraded_reasons,
        policy=freeze_policy,
    )
    reviewed = CompleteReviewStages(
        (model_stage.snapshot, scored_stage.snapshot, risk_stage.snapshot),
        ScoredStageContext(
            freeze_policy,
            native_input.evaluated_at,
            freeze_request.review_candidate_codes,
            (),
            f"local:{sequence}",
            fixed,
        ),
        runtime.monotonic,
    )
    stages = ScoredStageChain(
        model_stage, scored_stage, risk_stage, fixed, reviewed.risk_review, reviewed.action_publish, reviewed.entries,
    )
    epoch = BuildScoredFreezeEpoch(freeze_request, entries=stages.entries)
    model_version = model_batch.model_version if model_batch is not None else None
    downside_assessments = tuple((item.code, risk_control.assess(item.features, strategy)) for item in epoch.entries)
    return ScoredLocalProjection(
        native_input=native_input,
        stages=stages,
        selection=selection,
        input_quality=quality,
        review_candidates=candidates,
        local_epoch=epoch,
        local=_scored_freeze(
            epoch,
            _FreezeProjectionContext(
                input_version=native_input.input_version,
                strategy=strategy,
                freeze_policy=freeze_policy,
                downside_assessments=downside_assessments,
                score_model_version=model_version,
                model_diagnostics=model_batch.diagnostics if model_batch is not None else None,
                no_positive_model_utility=(
                    _no_positive_model_utility(model_batch.diagnostics.values()) if model_batch is not None else False
                ),
                cost_reference_version=model_batch.cost_reference_version if model_batch is not None else None,
                score_scale=model_batch.score_scale if model_batch is not None else WEIGHTED_EVIDENCE_SCORE_SCALE,
                score_model_id=model_batch.score_model_id if model_batch is not None else None,
                score_contract_hash=model_batch.score_contract_hash if model_batch is not None else None,
                production_authority=production_authority,
                rank_population_version=model_batch.rank_population_version if model_batch is not None else None,
                preview_context=native_input.preview_context,
                official_close_evidence=official_close_evidence,
                prediction_target=target,
                scoring_profile=(
                    head_status.profile_id
                    if strategy is Strategy.TOMORROW and target is not None and head_status is not None else None
                ),
            ),
        ),
        score_model_version=model_version,
        model_diagnostics=tuple(sorted(model_batch.diagnostics.items())) if model_batch is not None else (),
        downside_assessments=downside_assessments,
        cost_reference_version=model_batch.cost_reference_version if model_batch is not None else None,
        score_scale=model_batch.score_scale if model_batch is not None else WEIGHTED_EVIDENCE_SCORE_SCALE,
        score_model_id=model_batch.score_model_id if model_batch is not None else None,
        score_contract_hash=model_batch.score_contract_hash if model_batch is not None else None,
        production_authority=production_authority,
        rank_population_version=model_batch.rank_population_version if model_batch is not None else None,
    )


def _local_score_stage(
    source: PipelineStageOutput[FeatureSnapshot],
    selection: ScoredSelectionResult,
    monotonic: Callable[[], float],
    started: float,
    scoring_identity: str,
    *,
    operation_durations: tuple[tuple[str, int | None], ...],
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
    return MeasuredOutput(
        StageOutput(
            PipelineStage.LOCAL_SCORE,
            scored,
            input_batch_id=source.snapshot.output_batch_id,
            output_batch_id=f"{source.snapshot.output_batch_id}:local_score:{scoring_identity}",
            as_of=source.snapshot.as_of,
            input_count=len(source.records),
            reasons=ReasonAggregates(reasons),
            pending_count=sum(
                count
                for reason, count in reasons.items()
                if reason
                in {"production_model_features_missing", "strategy_history_insufficient"} | HISTORY_INPUT_FAILURES
            ),
            source_health=source.snapshot.source_health,
            latency_ms=0,
        ),
        started,
        monotonic,
        operation_durations=operation_durations,
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
        if isinstance(diagnostics, ModelDiagnostics) and diagnostics.predicted_net_excess_pct <= 0.0
    }


def _model_eligible_features(
    features: tuple[FeatureSnapshot, ...],
    policy: RecommendationPolicy,
    *,
    evaluated_at: datetime,
    max_age_seconds: float,
) -> tuple[FeatureSnapshot, ...]:
    eligible: list[FeatureSnapshot] = []
    rules = DefaultFilterRules(max_age_seconds=max_age_seconds, policy=policy.hard_filter)
    for feature in features:
        filtered = ApplyFilters(feature, rules, now=evaluated_at)
        if filtered.allowed:
            eligible.append(
                feature
                if feature.quote.board is filtered.board
                else replace(feature, quote=replace(feature.quote, board=filtered.board))
            )
    return tuple(eligible)


def _market_population_watermark(features: tuple[FeatureSnapshot, ...]) -> datetime:
    return max(
        value
        for feature in features
        for value in (feature.observed_at, feature.quote.source_time, feature.quote.received_time)
    )


def BuildScoredHybrid(
    projection: ScoredReviewDraft,
    policy: RecommendationPolicy,
    reviews: Mapping[str, DeepSeekReview],
    *,
    review_deadline: datetime,
    monotonic: Callable[[], float] = time.monotonic,
    review_latency_ms: int = 0,
) -> ScoredReviewProjection | None:
    strategy = projection.local.strategy
    if projection.native_strategy is not strategy:
        return None
    freeze_policy = BuildScoredFreezePolicy(policy, strategy, phase=projection.phase)
    candidates = {item.code for item in projection.review_candidates}
    if any(code not in candidates or review.code != code for code, review in reviews.items()):
        return None
    normalized = NormalizeScoredReviewTimes(reviews, review_deadline)
    if normalized is None:
        return None
    usable = {
        code: review
        for code, review in normalized.items()
        if review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN} and review.completed_at < review_deadline
    }
    observed_at = max(
        (
            projection.evaluated_at,
            *(review.completed_at for review in normalized.values()),
            *(
                review.evidence_binding.bound_at
                for review in normalized.values()
                if review.evidence_binding is not None
            ),
        )
    )
    stages = CompleteReviewStages(
        projection.prior_snapshots,
        ScoredStageContext(
            freeze_policy,
            observed_at,
            tuple(item.code for item in projection.review_candidates),
            tuple(normalized.values()),
            f"review:{projection.local.sequence + 1}",
            projection.fixed_selection,
            review_latency_ms=review_latency_ms,
            review_attempted=True,
        ),
        monotonic,
    )
    if not usable:
        return ScoredReviewProjection(None, stages)
    review_codes = ValidateScoredFreezeReviews(
        tuple(item.code for item in projection.review_candidates),
        projection.fixed_selection.records,
        normalized,
        observed_at=observed_at,
        projection_stage="hybrid",
    )
    epoch = replace(
        projection.local_epoch,
        observed_at=observed_at,
        sequence=projection.local.sequence + 1,
        strategy_version=policy.strategy_version,
        research_epoch_version=None,
        projection_stage="hybrid",
        parent_freeze_version=projection.local_epoch.version,
        entries=stages.entries,
        review_candidate_codes=review_codes,
        degraded_reasons=tuple(
            sorted(
                {
                    *projection.input_quality.degraded_reasons,
                    *(() if set(usable) == candidates else ("deepseek_incomplete",)),
                }
            )
        ),
        selection_limits=FreezeSelectionLimits(
            freeze_policy.top_k, freeze_policy.observation_limit, freeze_policy.maximum_board_fraction,
        ),
    )
    local_versions = dict(projection.local.input_versions)
    target_identity = local_versions.get("prediction_target")
    freeze = _scored_freeze(
        epoch,
        _FreezeProjectionContext(
            input_version=projection.input_version,
            downside_assessments=projection.downside_assessments,
            parent_version=projection.local.version,
            strategy=strategy,
            freeze_policy=freeze_policy,
            score_model_version=projection.score_model_version,
            model_diagnostics=dict(projection.model_diagnostics),
            no_positive_model_utility=projection.no_positive_model_utility,
            cost_reference_version=projection.cost_reference_version,
            score_scale=projection.score_scale,
            score_model_id=projection.score_model_id,
            score_contract_hash=projection.score_contract_hash,
            production_authority=projection.production_authority,
            rank_population_version=projection.rank_population_version,
            preview_context=projection.local.preview_context,
            prediction_target=ParsePredictionTargetIdentity(target_identity) if target_identity != TOMORROW_CLOSE_GAIN_TARGET else None,
            scoring_profile=local_versions.get("scoring_profile"),
        ),
    )
    return ScoredReviewProjection(replace(freeze, input_versions=projection.local.input_versions), stages)


def _scored_freeze(
    epoch: FreezeEpoch,
    context: _FreezeProjectionContext,
) -> ScoredFreeze:
    downside_assessments = dict(context.downside_assessments)
    execution_versions: tuple[tuple[str, str], ...] = ()
    if not context.production_authority:
        execution_versions = (("production_authority", "false"), ("execution_mode", "shadow"))
    elif context.score_scale in MODEL_SELECTION_SCORE_SCALES:
        execution_versions = (("production_authority", "true"), ("execution_mode", "production"))
    return ScoredFreeze(
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
                ("score_scale", context.score_scale),
                ("score_model", context.score_model_version),
                ("score_model_id", context.score_model_id),
                ("score_contract_hash", context.score_contract_hash),
                ("rank_population", context.rank_population_version),
                ("cost_reference", context.cost_reference_version),
                (
                    "prediction_target",
                    context.prediction_target.identity
                    if context.prediction_target is not None
                    else TOMORROW_CLOSE_GAIN_TARGET if context.score_scale == TOMORROW_PROBABILITY_SCORE_SCALE else None,
                ),
                ("scoring_profile", context.scoring_profile),
                (f"{context.strategy.value}_close_evidence", context.official_close_evidence),
            )
            if value is not None
        )
        + execution_versions
        + tuple((f"board_population:{board}", version) for board, version in sorted(epoch.population_versions.items())),
        config_version=epoch.config_version,
        strategy_version=epoch.strategy_version,

        items=tuple(
            _freeze_item(
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
        preview_context=context.preview_context,
    )


def _freeze_item(
    entry: ScoredFreezeEntry,
    *,
    strategy: Strategy,
    review_eligible: bool,
    model_diagnostics: ModelDiagnostics | TomorrowProbability | None,
    downside: DownsideAssessment,
) -> FreezeItem:
    reason = entry.freeze_skip_reason or entry.action_reason or "not_selected"
    return FreezeItem(
        score_ledger=entry.score_ledger,
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
        risk_details=(
            *(FreezeRiskDisclosure(fact.risk_code, "local", fact.veto) for fact in entry.local_risk_facts),
            *(FreezeRiskDisclosure(fact.risk_code, "deepseek", fact.veto) for fact in entry.deepseek_risk_facts),
        ),
        reason=reason,
        board=entry.features.quote.board,
        selection_rank=entry.selection_rank,
        name=entry.features.quote.name,
        industry=entry.features.quote.industry,
        quote=_freeze_quote(entry.features.quote),
        setup_type=downside.setup_type,
        downside=FreezeDownside(
            downside.status,
            downside.reasons,
            downside.atr20_pct,
            downside.intraday_reversal_atr,
            downside.historical_drawdown_pct,
        ),
        review_outcome=entry.review_outcome.value if entry.review_outcome is not None else None,
        research_coverage=FreezeResearchCoverage(
            len(entry.features.evidence),
            len(entry.features.external_risk_facts),
            review_eligible,
            entry.features.research_components,
        ),
        model_diagnostics=(
            FreezeModelDiagnostics(
                signal_score=model_diagnostics.signal_score,
                predicted_excess_return_pct=model_diagnostics.predicted_excess_return_pct,
                estimated_cost_pct=model_diagnostics.estimated_cost_pct,
                predicted_net_excess_pct=model_diagnostics.predicted_net_excess_pct,
                model_disagreement_pct=model_diagnostics.model_disagreement_pct,
                target_probability=model_diagnostics.target_probability,
            )
            if isinstance(model_diagnostics, ModelDiagnostics)
            else None
        ),
    )


def _selection_diagnostics(
    epoch: FreezeEpoch,
    context: _FreezeProjectionContext,
) -> SelectionDiagnostics:
    policy = context.freeze_policy
    selected = tuple(item for item in epoch.entries if item.selected)
    maximum_final_score = max((item.score.final_score for item in epoch.entries), default=None)
    maximum_action_score = (
        max((item.score.local_score for item in epoch.entries), default=None)
        if context.score_scale == TOMORROW_PROBABILITY_SCORE_SCALE else maximum_final_score
    )
    observation_floor = max(0.0, policy.executable_threshold - policy.observation_margin)
    empty_reason = None
    if context.no_positive_model_utility:
        empty_reason = "no_positive_net_utility"
    elif not epoch.entries:
        empty_reason = "no_scored_candidates"
    elif not selected:
        if maximum_action_score is not None and maximum_action_score < observation_floor:
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
        evaluated_count=epoch.evaluated_count - epoch.unscored_count,
    )


def _no_positive_model_utility(
    diagnostics: Iterable[ModelDiagnostics | TomorrowProbability],
) -> bool:
    items = iter(diagnostics)
    first = next(items, None)
    return isinstance(first, ModelDiagnostics) and first.predicted_net_excess_pct <= 0.0 and all(
        isinstance(item, ModelDiagnostics) and item.predicted_net_excess_pct <= 0.0 for item in items
    )


def _official_close_evidence(
    native_input: ScoredNativeInput, qualified: tuple[FeatureSnapshot, ...]
) -> str | None:
    cutoff = datetime.combine(native_input.trade_date, wall_time(15), tzinfo=native_input.evaluated_at.tzinfo)
    if native_input.preview_context is not None or native_input.evaluated_at < cutoff:
        return None
    if not any(
        IsTomorrowClosingQuote(item.quote, native_input.trade_date, native_input.evaluated_at)
        for item in native_input.market_features
    ) or any(
        not IsTomorrowClosingQuote(item.quote, native_input.trade_date, native_input.evaluated_at)
        for item in qualified
    ):
        return None
    for item in (*native_input.market_features, *qualified):
        if item.company_context is not None and item.company_context.facts.published_at > cutoff:
            return None
        if any(
            evidence.evidence_type != "structured_point_in_time"
            and (evidence.published_at > cutoff or evidence.received_at is not None and evidence.received_at > cutoff)
            for evidence in item.evidence
        ):
            return None
    return f"official-close:{native_input.input_version}"


def _freeze_quote(quote: MarketQuote) -> FreezeQuote:
    if quote.price is None:
        raise ValueError("selected freeze quote price is unavailable")
    return FreezeQuote(
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


def ValidateReviewManifests(
    candidates: tuple[ScoredReviewCandidate, ...],
    reviews: Mapping[str, DeepSeekReview],
    expected: Mapping[str, str],
) -> bool:
    features = {item.code: item.features for item in candidates}
    return not any(
        code not in features
        or review.code != code
        or (
            review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN}
            and not _review_manifest_matches(review, expected.get(code), features[code].observed_at)
        )
        for code, review in reviews.items()
    )


def _review_manifest_matches(review: DeepSeekReview, expected: str | None, observed_at: datetime) -> bool:
    binding = review.evidence_binding
    if binding is None:
        return review.evidence_manifest_hash == expected and expected is not None
    return (
        review.cache_reused
        and binding.code == review.code
        and binding.original_manifest_hash == review.evidence_manifest_hash
        and binding.current_manifest_hash == expected
        and binding.original_completed_at == review.completed_at
        and binding.bound_at >= observed_at
    )


__all__ = [
    "ScoredLocalProjection",
    "ScoredReviewDraft",
    "BuildScoredHybrid",
    "BuildScoredLocal",
    "PrepareScoredReviewDraft",
    "ValidateReviewManifests",
]
