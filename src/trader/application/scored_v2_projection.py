"""Native scored input projection to the unified V2 decision identity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from trader.application.policy import RecommendationPolicy
from trader.application.ports.scored import ScoredNativeInput
from trader.application.recommendation_policy_codec import preselection_replay_feature
from trader.application.scored_deepseek_fusion import (
    normalize_scored_review_times,
    v2_decision_policy,
)
from trader.application.scored_quality import ScoredInputQuality, assess_scored_input_quality
from trader.application.scored_selection import (
    ScoredSelectionIdentity,
    ScoredSelectionOptions,
    select_scored_features,
)
from trader.domain.market.models import MarketQuote
from trader.domain.recommendation.decision_identity import (
    DecisionDownside,
    DecisionItem,
    DecisionQuote,
    DecisionResearchCoverage,
    ScoredDecision,
    SelectionDiagnostics,
)
from trader.domain.recommendation.downside import assess_downside
from trader.domain.recommendation.models import RecommendationAction, Strategy
from trader.domain.recommendation.scored_fusion import (
    DecisionEpoch,
    ScoredDecisionEntry,
    ScoredDecisionPolicy,
    ScoredDecisionRequest,
    ScoredReviewCandidate,
    build_scored_decision_epoch,
    select_scored_review_candidates,
)
from trader.domain.recommendation.scored_selection import ScoredSelectionResult
from trader.domain.review.models import DeepSeekReview, ReviewOutcome


@dataclass(frozen=True)
class ScoredV2LocalProjection:
    native_input: ScoredNativeInput
    selection: ScoredSelectionResult
    input_quality: ScoredInputQuality
    review_candidates: tuple[ScoredReviewCandidate, ...]
    local_epoch: DecisionEpoch
    local: ScoredDecision


def build_scored_v2_local(
    native_input: ScoredNativeInput,
    policy: RecommendationPolicy,
    *,
    sequence: int,
) -> ScoredV2LocalProjection:
    if sequence < 1:
        raise ValueError("scored V2 decision sequence must be positive")
    strategy = native_input.strategy
    decision_policy = v2_decision_policy(policy, strategy, phase=native_input.phase)
    selection = select_scored_features(
        tuple(preselection_replay_feature(feature) for feature in native_input.market_features),
        policy,
        ScoredSelectionOptions(
            evaluated_at=native_input.evaluated_at,
            max_age_seconds=native_input.score_max_age_seconds,
            phase=native_input.phase,
            candidate_features=native_input.candidate_features,
            normalize_discovery_source_time=True,
            strategy=strategy,
        ),
        ScoredSelectionIdentity(
            trade_date=native_input.trade_date,
            data_version=native_input.data_version,
            merge_epoch=native_input.input_version,
        ),
    )
    quality = assess_scored_input_quality(native_input, selection)
    candidates = select_scored_review_candidates(selection, decision_policy)
    input_hash = native_input.input_version.removeprefix("native-input:")
    epoch = build_scored_decision_epoch(
        ScoredDecisionRequest(
            selection=selection,
            reviews={},
            observed_at=native_input.evaluated_at,
            trade_date=native_input.trade_date,
            sequence=sequence,
            config_version=native_input.config_version,
            strategy_version=policy.strategy_version,
            fusion_version=policy.fusion_version,
            market_epoch_version=f"native-market:{input_hash}",
            candidate_epoch_version=(f"native-candidate:{input_hash}" if native_input.candidate_features else None),
            research_epoch_version=None,
            projection_stage="local",
            parent_decision_version=None,
            review_candidate_codes=tuple(item.code for item in candidates),
            degraded_reasons=quality.degraded_reasons,
            policy=decision_policy,
        )
    )
    return ScoredV2LocalProjection(
        native_input,
        selection,
        quality,
        candidates,
        epoch,
        _scored_decision(
            epoch,
            input_version=native_input.input_version,
            strategy=strategy,
            decision_policy=decision_policy,
        ),
    )


def build_scored_v2_hybrid(
    projection: ScoredV2LocalProjection,
    policy: RecommendationPolicy,
    reviews: Mapping[str, DeepSeekReview],
    *,
    review_deadline: datetime,
) -> ScoredDecision | None:
    strategy = projection.local.strategy
    if projection.native_input.strategy is not strategy:
        return None
    decision_policy = v2_decision_policy(policy, strategy, phase=projection.native_input.phase)
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
    if not usable:
        return None
    observed_at = max(
        projection.native_input.evaluated_at,
        *(review.completed_at for review in usable.values()),
    )
    epoch = build_scored_decision_epoch(
        ScoredDecisionRequest(
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
    )
    return _scored_decision(
        epoch,
        input_version=projection.native_input.input_version,
        parent_version=projection.local.version,
        strategy=strategy,
        decision_policy=decision_policy,
    )


def _scored_decision(
    epoch: DecisionEpoch,
    *,
    input_version: str,
    strategy: Strategy,
    decision_policy: ScoredDecisionPolicy,
    parent_version: str | None = None,
) -> ScoredDecision:
    return ScoredDecision(
        strategy=strategy,
        trade_date=epoch.trade_date,
        sequence=epoch.sequence,
        observed_at=epoch.observed_at,
        stage=epoch.projection_stage,
        parent_version=parent_version,
        input_versions=tuple(
            (name, value)
            for name, value in (
                ("native", input_version),
                ("market", epoch.market_epoch_version),
                ("candidate", epoch.candidate_epoch_version),
                ("research", epoch.research_epoch_version),
            )
            if value is not None
        ),
        config_version=epoch.config_version,
        strategy_version=epoch.strategy_version,
        fusion_version=epoch.fusion_version,
        items=tuple(
            _decision_item(
                item,
                strategy=strategy,
                review_eligible=item.code in epoch.review_candidate_codes,
            )
            for item in epoch.entries
        ),
        filter_aggregates=tuple(epoch.filter_reason_counts.items()),
        degraded_reasons=epoch.degraded_reasons,
        population_count=epoch.evaluated_count,
        rejected_count=epoch.rejected_count,
        selection_diagnostics=_selection_diagnostics(epoch, decision_policy),
    )


def _decision_item(
    entry: ScoredDecisionEntry,
    *,
    strategy: Strategy,
    review_eligible: bool,
) -> DecisionItem:
    reason = entry.decision_skip_reason or entry.action_reason or "not_selected"
    downside = assess_downside(entry.features, strategy)
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
    )


def _selection_diagnostics(
    epoch: DecisionEpoch,
    policy: ScoredDecisionPolicy,
) -> SelectionDiagnostics:
    selected = tuple(item for item in epoch.entries if item.selected)
    maximum_final_score = max((item.score.final_score for item in epoch.entries), default=None)
    observation_floor = max(0.0, policy.executable_threshold - policy.observation_margin)
    empty_reason = None
    if not epoch.entries:
        empty_reason = "no_scored_candidates"
    elif not selected:
        empty_reason = (
            "score_below_observation_floor"
            if maximum_final_score is not None and maximum_final_score < observation_floor
            else "risk_or_execution_blocked"
        )
    return SelectionDiagnostics(
        maximum_final_score,
        policy.executable_threshold,
        observation_floor,
        policy.top_k,
        policy.observation_limit,
        sum(item.action is RecommendationAction.EXECUTABLE for item in selected),
        sum(item.action is RecommendationAction.OBSERVE for item in selected),
        len(epoch.review_candidate_codes),
        empty_reason,
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
    projection: ScoredV2LocalProjection,
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
    "ScoredV2LocalProjection",
    "build_scored_v2_hybrid",
    "build_scored_v2_local",
    "validate_review_manifests",
]
