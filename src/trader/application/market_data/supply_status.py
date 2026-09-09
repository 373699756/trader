"""Typed projection of scored candidate quality into runtime supply diagnostics."""

from __future__ import annotations

import math
from collections import Counter

from trader.application.ports.runtime_status import InputQualityStatus, SupplyFunnel, SupplySummary
from trader.application.recommendation.scored_projection import ScoredLocalProjection
from trader.application.recommendation.scored_quality import ScoredInputQuality
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import RecommendationAction, ScoredDisposition
from trader.domain.recommendation.selection.scored_selection import ScoredCandidateStageCounts


def build_supply_status(
    projection: ScoredLocalProjection,
    candidate_stage_counts: ScoredCandidateStageCounts | None = None,
    *,
    candidate_quote_eligible: int | None = None,
) -> InputQualityStatus:
    quality = projection.input_quality
    diagnostics = projection.local.selection_diagnostics
    if diagnostics is None:
        raise ValueError("scored input status requires selection diagnostics")
    requested = set(projection.native_input.requested_codes)
    evaluations = tuple(item for item in projection.selection.evaluations if item.code in requested)
    decision_items = projection.local.items
    stage_counts = candidate_stage_counts or ScoredCandidateStageCounts(
        issuer_eligible_population=quality.population_count,
        dynamic_filter_eligible=max(0, quality.population_count - quality.population_rejected_count),
        strategy_history_eligible=quality.history_covered_count,
        model_input_eligible=quality.history_covered_count,
        candidate_score_eligible=quality.candidate_scored_count,
        candidate_limit_selected=quality.candidate_scored_count,
    )
    funnel = SupplyFunnel(
        issuer_eligible_population=stage_counts.issuer_eligible_population,
        dynamic_filter_eligible=stage_counts.dynamic_filter_eligible,
        strategy_history_eligible=stage_counts.strategy_history_eligible,
        model_input_eligible=stage_counts.model_input_eligible,
        candidate_score_eligible=stage_counts.candidate_score_eligible,
        candidate_limit_selected=stage_counts.candidate_limit_selected,
        candidate_quote_eligible=(
            quality.candidate_feature_count if candidate_quote_eligible is None else candidate_quote_eligible
        ),
        requested_candidates=quality.candidate_count,
        candidate_features=quality.candidate_feature_count,
        security_master=quality.security_master_covered_count,
        history=quality.history_covered_count,
        filter_pass=sum(item.disposition is ScoredDisposition.PASS for item in evaluations),
        filter_observe=sum(item.disposition is ScoredDisposition.OBSERVE_ONLY for item in evaluations),
        filter_reject=sum(item.disposition is ScoredDisposition.REJECT for item in evaluations),
        full_scored=quality.candidate_scored_count,
        review_eligible=len(projection.review_candidates),
        observation_threshold_met_count=sum(
            item.final_score >= diagnostics.observation_floor for item in decision_items
        ),
        executable_threshold_met_count=sum(
            item.final_score >= diagnostics.executable_threshold for item in decision_items
        ),
        action_executable=sum(item.action is RecommendationAction.EXECUTABLE for item in decision_items),
        action_observe=sum(item.action is RecommendationAction.OBSERVE for item in decision_items),
        action_unavailable=sum(item.action is RecommendationAction.UNAVAILABLE for item in decision_items),
        selected_executable=sum(
            item.selected and item.action is RecommendationAction.EXECUTABLE for item in decision_items
        ),
        selected_observe=sum(item.selected and item.action is RecommendationAction.OBSERVE for item in decision_items),
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
    return InputQualityStatus(
        strategy=projection.local.strategy,
        status=quality.status,
        publishable=quality.publishable,
        summary=_supply_summary(projection),
        supply_funnel=funnel,
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
        supply_reason_counts=tuple(reasons.items()),
        primary_blocker=_primary_supply_blocker(quality, funnel, empty_reason=diagnostics.empty_reason),
    )


def _supply_summary(projection: ScoredLocalProjection) -> SupplySummary:
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
    highest = max((item.final_score for item in projection.local.items), default=None)
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
    funnel: SupplyFunnel,
    *,
    empty_reason: str | None,
) -> str:
    action_blocker = "no_executable_candidates" if funnel.action_observe else "local_score_below_observation_floor"
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
            and funnel.full_scored == 0
            and funnel.strategy_history_eligible < funnel.dynamic_filter_eligible,
            "strategy_history_unavailable",
        ),
        (funnel.full_scored == 0, "no_scored_candidates"),
        (empty_reason == "no_positive_net_utility", "no_positive_net_utility"),
        (funnel.review_eligible == 0, "no_review_eligible_candidates"),
        (funnel.action_executable == 0, action_blocker),
        (funnel.selected_executable == 0, "selection_constraints"),
    )
    return next((reason for blocked, reason in priorities if blocked), "ready")


__all__ = ["build_supply_status"]
