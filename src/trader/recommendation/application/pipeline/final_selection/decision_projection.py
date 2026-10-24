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
    CompleteScoredStages,
    FixScoredSeats,
    ReasonAggregates,
    ScoredStageChain,
    ScoredStageContext,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.pipeline.policy_projection import PreselectionReplayFeature
from trader.recommendation.application.pipeline.quality_check.input_quality_service import (
    AssessScoredInputQuality,
    PrepareNativeQualityBatch,
    QualityScoringBatch,
    ScoredInputQuality,
    ScoredInputQualityOptions,
)
from trader.recommendation.application.pipeline.risk_review.deepseek_evidence_gate import (
    BuildScoredDecisionPolicy,
    NormalizeScoredReviewTimes,
)
from trader.recommendation.application.pipeline.stage_output import MeasuredOutput, PipelineStageOutput, StageOutput
from trader.recommendation.application.ports.loaded_profile import (
    ModelDiagnostics,
    ModelScoreBatch,
    ModelScoringContext,
    ModelScoringPort,
)
from trader.recommendation.application.ports.scoring import ScoredNativeInput
from trader.recommendation.domain.candidate.composition import WEIGHTED_EVIDENCE_SCORE_SCALE
from trader.recommendation.domain.candidate.filters import HardFilter
from trader.recommendation.domain.evidence.pipeline import PipelineStage
from trader.recommendation.domain.evidence.review import DeepSeekReview, ReviewOutcome
from trader.recommendation.domain.market.history_alignment import HISTORY_INPUT_FAILURES
from trader.recommendation.domain.market.models import FeatureSnapshot, MarketQuote
from trader.recommendation.domain.publication.decision_identity import (
    DecisionDownside,
    DecisionItem,
    DecisionModelDiagnostics,
    DecisionQuote,
    DecisionResearchCoverage,
    DecisionRiskDisclosure,
    ScoredDecision,
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
    BuildScoredDecisionEpoch,
    DecisionEpoch,
    ScoredDecisionEntry,
    ScoredDecisionPolicy,
    ScoredDecisionRequest,
    ScoredReviewCandidate,
    SelectScoredReviewCandidates,
)
from trader.recommendation.domain.selection.scored_selection import ApplySelectionRisks, ScoredCandidateStageCounts


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
    cost_reference_version: str | None = None
    started_monotonic: float | None = None
    score_scale: str = WEIGHTED_EVIDENCE_SCORE_SCALE
    score_model_id: str | None = None
    score_contract_hash: str | None = None
    production_authority: bool = True
    rank_population_version: str | None = None


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
    cost_reference_version: str | None = None
    score_scale: str = WEIGHTED_EVIDENCE_SCORE_SCALE
    score_model_id: str | None = None
    score_contract_hash: str | None = None
    production_authority: bool = True
    rank_population_version: str | None = None
    preview_context: PreviewContext | None = None


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


def BuildScoredLocal(
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
        quality_batch = PrepareNativeQualityBatch(native_input, policy.hard_filter, minimum_history, runtime.monotonic)
    else:
        quality_batch = runtime.quality_batch
        if quality_batch.native_input != native_input:
            raise ValueError("scoring context does not match its quality batch")
    native_input = quality_batch.native_input
    scoring_input = replace(native_input, candidate_features=quality_batch.output.records)
    started = runtime.monotonic()
    strategy = native_input.strategy
    decision_policy = BuildScoredDecisionPolicy(policy, strategy, phase=native_input.phase)
    risk_control = runtime.risk_control if runtime.risk_control is not None else RiskControlService()
    ranking_selection = (
        runtime.ranking_selection if runtime.ranking_selection is not None else RankingSelectionService()
    )
    population = tuple(PreselectionReplayFeature(feature) for feature in native_input.market_features)
    model_scoring = runtime.model_scoring
    profile_status = model_scoring.status() if model_scoring is not None else None
    learned_rank = profile_status is not None and profile_status.profile_id == "v1"
    uses_model = (
        model_scoring is not None
        and (native_input.phase != "close_fallback" or learned_rank)
        and model_scoring.uses_model(strategy)
    )
    model_batch = (
        model_scoring.score(
            strategy,
            _model_eligible_features(
                scoring_input.candidate_features,
                policy,
                evaluated_at=native_input.evaluated_at,
                max_age_seconds=native_input.score_max_age_seconds,
            ),
            context=replace(
                runtime.scoring_context or ModelScoringContext(),
                population_features=_model_eligible_features(
                    native_input.market_features,
                    policy,
                    evaluated_at=(
                        _market_population_watermark(native_input.market_features)
                        if native_input.market_features
                        else native_input.evaluated_at
                    ),
                    max_age_seconds=native_input.preselect_max_age_seconds,
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
    production_authority = (profile_status is None or profile_status.production_authority) and (
        model_batch is None or model_batch.production_authority
    )
    if learned_rank and (
        model_batch is None
        or model_batch.score_scale != "learned_rank_percentile_0_100"
        or model_batch.production_authority
    ):
        raise ValueError("learned_rank_shadow_model_unavailable")
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
    )
    scored_stage = _local_score_stage(
        model_stage, selection, runtime.monotonic, started, f"{native_input.input_version}:{sequence}"
    )
    started = runtime.monotonic()
    selection = ApplySelectionRisks(
        selection, policy.risk_rules, policy.fusion.local_risk_cap, native_input.evaluated_at, strategy
    )
    if not production_authority:
        ranked = {
            item.code: replace(
                item,
                ranking_score=(
                    model_batch.diagnostics[item.code].signal_score
                    if learned_rank and model_batch is not None
                    else item.ranking_score
                ),
                shadow_only=True,
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
    fixed = FixScoredSeats(risk_stage, decision_policy, native_input.evaluated_at, runtime.monotonic)
    candidates = SelectScoredReviewCandidates(fixed.records, decision_policy) if production_authority else ()
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
    stages = CompleteScoredStages(
        model_stage,
        scored_stage,
        risk_stage,
        ScoredStageContext(
            decision_policy,
            native_input.evaluated_at,
            decision_request.review_candidate_codes,
            (),
            f"local:{sequence}",
            fixed,
        ),
        runtime.monotonic,
    )
    epoch = BuildScoredDecisionEpoch(decision_request, entries=stages.entries)
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
                cost_reference_version=model_batch.cost_reference_version if model_batch is not None else None,
                score_scale=model_batch.score_scale if model_batch is not None else WEIGHTED_EVIDENCE_SCORE_SCALE,
                score_model_id=model_batch.score_model_id if model_batch is not None else None,
                score_contract_hash=model_batch.score_contract_hash if model_batch is not None else None,
                production_authority=production_authority,
                rank_population_version=model_batch.rank_population_version if model_batch is not None else None,
                preview_context=native_input.preview_context,
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


def _model_eligible_features(
    features: tuple[FeatureSnapshot, ...],
    policy: RecommendationPolicy,
    *,
    evaluated_at: datetime,
    max_age_seconds: float,
) -> tuple[FeatureSnapshot, ...]:
    eligible: list[FeatureSnapshot] = []
    for feature in features:
        filtered = HardFilter(
            feature,
            evaluated_at,
            max_age_seconds=max_age_seconds,
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


def BuildScoredHybrid(
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
    decision_policy = BuildScoredDecisionPolicy(policy, strategy, phase=projection.native_input.phase)
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
            projection.native_input.evaluated_at,
            *(review.completed_at for review in normalized.values()),
            *(
                review.evidence_binding.bound_at
                for review in normalized.values()
                if review.evidence_binding is not None
            ),
        )
    )
    stages = CompleteScoredStages(
        projection.stages.model_filter,
        projection.stages.local_score,
        projection.stages.local_risk,
        ScoredStageContext(
            decision_policy,
            observed_at,
            tuple(item.code for item in projection.review_candidates),
            tuple(normalized.values()),
            f"review:{projection.local.sequence + 1}",
            projection.stages.fixed_selection,
            review_latency_ms=review_latency_ms,
            review_attempted=True,
        ),
        monotonic,
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
    epoch = BuildScoredDecisionEpoch(decision_request, entries=stages.entries)
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
            cost_reference_version=projection.cost_reference_version,
            score_scale=projection.score_scale,
            score_model_id=projection.score_model_id,
            score_contract_hash=projection.score_contract_hash,
            production_authority=projection.production_authority,
            rank_population_version=projection.rank_population_version,
            preview_context=projection.native_input.preview_context,
        ),
    )
    return ScoredReviewProjection(replace(decision, input_versions=projection.local.input_versions), stages)


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
                ("score_scale", context.score_scale),
                ("score_model", context.score_model_version),
                ("score_model_id", context.score_model_id),
                ("score_contract_hash", context.score_contract_hash),
                ("production_authority", "false" if not context.production_authority else None),
                ("execution_mode", "shadow" if not context.production_authority else None),
                ("rank_population", context.rank_population_version),
                ("cost_reference", context.cost_reference_version),
            )
            if value is not None
        )
        + tuple((f"board_population:{board}", version) for board, version in sorted(epoch.population_versions.items())),
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
        preview_context=context.preview_context,
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
        risk_details=(
            *(DecisionRiskDisclosure(fact.risk_code, "local", fact.veto) for fact in entry.local_risk_facts),
            *(DecisionRiskDisclosure(fact.risk_code, "deepseek", fact.veto) for fact in entry.deepseek_risk_facts),
        ),
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
            entry.features.research_components,
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
    if _has_no_positive_model_utility(epoch, context):
        empty_reason = "no_positive_net_utility"
    elif not epoch.entries:
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
        evaluated_count=epoch.evaluated_count - epoch.unscored_count,
    )


def _has_no_positive_model_utility(
    epoch: DecisionEpoch,
    context: _DecisionProjectionContext,
) -> bool:
    return (
        context.model_diagnostics is not None
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


def ValidateReviewManifests(
    projection: ScoredLocalProjection,
    reviews: Mapping[str, DeepSeekReview],
    expected: Mapping[str, str],
) -> bool:
    candidates = {item.code: item.features for item in projection.review_candidates}
    return not any(
        code not in candidates
        or review.code != code
        or (
            review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN}
            and not _review_manifest_matches(review, expected.get(code), candidates[code].observed_at)
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
    "BuildScoredHybrid",
    "BuildScoredLocal",
    "ValidateReviewManifests",
]
