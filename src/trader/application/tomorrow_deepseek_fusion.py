"""Read-once tomorrow local decision and optional DeepSeek hybrid upgrade."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime

from trader.application.policy import RecommendationPolicy
from trader.application.ports.market import RealtimeDataPlaneReaderPort
from trader.application.ports.reviews import (
    DeepSeekReviewUnavailableError,
    TomorrowDeepSeekReviewPort,
)
from trader.application.tomorrow_selection import (
    TomorrowSelectionOptions,
    select_tomorrow_snapshot,
)
from trader.domain.market.models import Board
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.tomorrow_fusion import (
    DecisionEpoch,
    TomorrowDecisionPolicy,
    TomorrowDecisionRequest,
    TomorrowReviewCandidate,
    build_tomorrow_decision_epoch,
    select_tomorrow_review_candidates,
)
from trader.domain.recommendation.tomorrow_selection import (
    BoardCrossSectionFallback,
    TomorrowSelectionResult,
)
from trader.domain.review.models import DeepSeekReview, ReviewOutcome


@dataclass(frozen=True)
class TomorrowDeepSeekFusionResult:
    local_decision: DecisionEpoch
    hybrid_decision: DecisionEpoch | None
    review_candidate_codes: tuple[str, ...]
    review_status: str


@dataclass(frozen=True)
class TomorrowDeepSeekFusionRequest:
    evaluated_at: datetime
    review_deadline: datetime
    max_age_seconds: float
    decision_sequence: int
    phase: str = "tomorrow"
    fallbacks: Mapping[Board, BoardCrossSectionFallback] | None = None


@dataclass(frozen=True)
class _FusionContext:
    request: TomorrowDeepSeekFusionRequest
    selection: TomorrowSelectionResult
    local: DecisionEpoch
    review_candidates: tuple[TomorrowReviewCandidate, ...]
    review_codes: tuple[str, ...]
    decision_policy: TomorrowDecisionPolicy
    market_epoch_version: str
    market_config_version: str
    market_trade_date: date
    candidate_version: str | None
    research_version: str | None


class TomorrowDeepSeekFusionUseCase:
    def __init__(
        self,
        reader: RealtimeDataPlaneReaderPort,
        reviewer: TomorrowDeepSeekReviewPort,
        policy: RecommendationPolicy,
    ) -> None:
        self._reader = reader
        self._reviewer = reviewer
        self._policy = policy

    def execute(
        self,
        request: TomorrowDeepSeekFusionRequest,
    ) -> TomorrowDeepSeekFusionResult:
        _require_shanghai_time(request.evaluated_at, "evaluated_at")
        _require_shanghai_time(request.review_deadline, "review_deadline")
        if request.decision_sequence < 0:
            raise ValueError("decision_sequence cannot be negative")
        snapshot = self._reader.snapshot()
        selection = select_tomorrow_snapshot(
            snapshot,
            self._policy,
            TomorrowSelectionOptions(
                evaluated_at=request.evaluated_at,
                max_age_seconds=request.max_age_seconds,
                phase=request.phase,
                fallbacks=request.fallbacks,
            ),
        )
        if snapshot.market is None:
            raise RuntimeError("selection returned without a market epoch")
        decision_policy = tomorrow_decision_policy(self._policy)
        review_candidates = select_tomorrow_review_candidates(selection, decision_policy)
        review_codes = tuple(item.code for item in review_candidates)
        candidate_version = _effective_epoch_version(
            snapshot.candidate_quotes.version if snapshot.candidate_quotes is not None else None,
            selection,
        )
        research_version = _effective_epoch_version(
            snapshot.research.version if snapshot.research is not None else None,
            selection,
        )
        local = build_tomorrow_decision_epoch(
            TomorrowDecisionRequest(
                selection=selection,
                reviews={},
                observed_at=request.evaluated_at,
                trade_date=snapshot.market.trade_date,
                sequence=request.decision_sequence,
                config_version=snapshot.market.config_version,
                strategy_version=self._policy.strategy_version,
                fusion_version=self._policy.fusion_version,
                market_epoch_version=snapshot.market.version,
                candidate_epoch_version=candidate_version,
                research_epoch_version=research_version,
                projection_stage="local",
                parent_decision_version=None,
                review_candidate_codes=review_codes,
                degraded_reasons=(),
                policy=decision_policy,
            )
        )
        if not review_candidates:
            result = TomorrowDeepSeekFusionResult(
                local,
                None,
                review_codes,
                "deepseek_skipped_no_eligible_candidates",
            )
        elif request.review_deadline <= request.evaluated_at:
            result = TomorrowDeepSeekFusionResult(local, None, review_codes, "deepseek_deadline_reached")
        else:
            result = self._upgrade_hybrid(
                _FusionContext(
                    request=request,
                    selection=selection,
                    local=local,
                    review_candidates=review_candidates,
                    review_codes=review_codes,
                    decision_policy=decision_policy,
                    market_epoch_version=snapshot.market.version,
                    market_config_version=snapshot.market.config_version,
                    market_trade_date=snapshot.market.trade_date,
                    candidate_version=candidate_version,
                    research_version=research_version,
                )
            )
        return result

    def _upgrade_hybrid(self, context: _FusionContext) -> TomorrowDeepSeekFusionResult:
        request = context.request
        review_candidates = context.review_candidates
        review_codes = context.review_codes
        contexts = {item.code: item.context for item in review_candidates}
        manifest_hashes = {
            item.code: self._reviewer.evidence_manifest_hash(item.features) for item in review_candidates
        }
        try:
            returned = dict(
                self._reviewer.review(
                    Strategy.TOMORROW,
                    tuple(item.features for item in review_candidates),
                    phase=request.phase,
                    deadline=request.review_deadline,
                    contexts=contexts,
                )
            )
        except DeepSeekReviewUnavailableError:
            return TomorrowDeepSeekFusionResult(context.local, None, review_codes, "deepseek_transport_failed")
        identity_error = _review_identity_error(returned, set(review_codes), manifest_hashes)
        if identity_error:
            return TomorrowDeepSeekFusionResult(context.local, None, review_codes, identity_error)
        normalized = normalize_tomorrow_review_times(returned, request.review_deadline)
        if normalized is None:
            return TomorrowDeepSeekFusionResult(
                context.local,
                None,
                review_codes,
                "deepseek_rejected_invalid_time",
            )
        usable = {
            code: review
            for code, review in normalized.items()
            if review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN}
        }
        if not usable:
            status = (
                "deepseek_late"
                if any(review.outcome is ReviewOutcome.LATE for review in normalized.values())
                else "deepseek_incomplete"
            )
            return TomorrowDeepSeekFusionResult(context.local, None, review_codes, status)
        complete = set(usable) == set(review_codes)
        degraded_reasons = () if complete else ("deepseek_incomplete",)
        observed_at = max(request.evaluated_at, *(review.completed_at for review in usable.values()))
        hybrid = build_tomorrow_decision_epoch(
            TomorrowDecisionRequest(
                selection=context.selection,
                reviews=normalized,
                observed_at=observed_at,
                trade_date=context.market_trade_date,
                sequence=request.decision_sequence + 1,
                config_version=context.market_config_version,
                strategy_version=self._policy.strategy_version,
                fusion_version=self._policy.fusion_version,
                market_epoch_version=context.market_epoch_version,
                candidate_epoch_version=context.candidate_version,
                research_epoch_version=context.research_version,
                projection_stage="hybrid",
                parent_decision_version=context.local.version,
                review_candidate_codes=review_codes,
                degraded_reasons=degraded_reasons,
                policy=context.decision_policy,
            )
        )
        return TomorrowDeepSeekFusionResult(
            context.local,
            hybrid,
            review_codes,
            "complete" if complete else "deepseek_incomplete",
        )


def tomorrow_decision_policy(policy: RecommendationPolicy) -> TomorrowDecisionPolicy:
    return TomorrowDecisionPolicy(
        dimension_weights=policy.dimension_weights[Strategy.TOMORROW],
        risk_rules=policy.risk_rules,
        executable_threshold=policy.selection.thresholds["tomorrow"],
        observation_margin=policy.selection.observation_margin,
        review_candidate_limit=min(policy.selection.review_candidate_limit, 28),
        top_k=min(policy.selection.default_top_k, 10),
        observation_limit=min(
            max(0, policy.selection.maximum_top_k - policy.selection.default_top_k),
            8,
        ),
        maximum_per_industry=policy.selection.maximum_per_industry,
        maximum_board_fraction=min(policy.selection.maximum_board_fraction, 0.60),
        fusion=policy.fusion,
    )


def _effective_epoch_version(
    version: str | None,
    selection: TomorrowSelectionResult,
) -> str | None:
    if version is None:
        return None
    return version if any(version in item.features.merge_epoch for item in selection.evaluations) else None


def _review_identity_error(
    reviews: Mapping[str, DeepSeekReview],
    candidates: set[str],
    manifest_hashes: Mapping[str, str],
) -> str:
    if any(code not in candidates or review.code != code for code, review in reviews.items()):
        return "deepseek_rejected_code_mismatch"
    if any(
        review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN}
        and review.evidence_manifest_hash != manifest_hashes[code]
        for code, review in reviews.items()
    ):
        return "deepseek_rejected_manifest_mismatch"
    return ""


def normalize_tomorrow_review_times(
    reviews: Mapping[str, DeepSeekReview],
    deadline: datetime,
) -> dict[str, DeepSeekReview] | None:
    normalized: dict[str, DeepSeekReview] = {}
    for code, review in reviews.items():
        if review.completed_at.tzinfo is None or review.completed_at.utcoffset() is None:
            return None
        if any(fact.observed_at.tzinfo is None or fact.observed_at.utcoffset() is None for fact in review.risk_facts):
            return None
        completed_at = review.completed_at.astimezone(deadline.tzinfo)
        risk_facts = tuple(
            replace(fact, observed_at=fact.observed_at.astimezone(deadline.tzinfo)) for fact in review.risk_facts
        )
        if any(fact.observed_at > completed_at for fact in risk_facts):
            return None
        is_late = completed_at > deadline
        normalized[code] = replace(
            review,
            outcome=ReviewOutcome.LATE if is_late else review.outcome,
            completed_at=completed_at,
            risk_facts=risk_facts,
            error="review_completed_after_deadline" if is_late else review.error,
        )
    return normalized


def _require_shanghai_time(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if getattr(value.tzinfo, "key", None) != "Asia/Shanghai":
        raise ValueError(f"{name} must use Asia/Shanghai")


__all__ = [
    "normalize_tomorrow_review_times",
    "tomorrow_decision_policy",
    "TomorrowDeepSeekFusionRequest",
    "TomorrowDeepSeekFusionResult",
    "TomorrowDeepSeekFusionUseCase",
]
