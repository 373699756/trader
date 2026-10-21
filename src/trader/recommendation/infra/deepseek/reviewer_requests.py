"""DeepSeek primary HTTP execution and repair mixin."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from trader.infra.cache_contracts import CacheRequestKey
from trader.recommendation.domain.evidence.review import (
    DeepSeekReview,
    ReviewOutcome,
)
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.deepseek.completion_client_contract import DeepSeekHttpResult
from trader.recommendation.infra.deepseek.reviewer_context import ReviewerContext
from trader.recommendation.infra.deepseek.reviewer_selection import (
    _combine_results,
    _in_deadline_timezone,
    _ReservationTracker,
    _usage_integer,
)
from trader.recommendation.infra.deepseek.reviewer_status import ReviewerStatusTracker
from trader.recommendation.infra.deepseek.schema import (
    BuildMessages,
    BuildRepairMessages,
    ClassifyReview,
    DeepSeekSchemaError,
    ParseReviews,
    StrategyReviewCacheKey,
)

_SUCCESSFUL_CANDIDATE_OUTCOMES = frozenset({ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN})


class ReviewerRequestExecutor:
    def __init__(self, context: ReviewerContext, status: ReviewerStatusTracker) -> None:
        self._settings = context.settings
        self._budget = context.budget
        self._client = context.client
        self._cache = context.cache
        self._dimension_weights = context.dimension_weights
        self._strategy_version = context.strategy_version
        self._confidence_coverage_min = context.confidence_coverage_min
        self._minimum_known_dimensions = context.minimum_known_dimensions
        self._now = context.now
        self._status = status

    def request_and_parse(
        self,
        candidates: Sequence[FeatureSnapshot],
        tracker: _ReservationTracker,
    ) -> tuple[dict[str, DeepSeekReview] | None, DeepSeekHttpResult, str]:
        first_offset = len(tracker.reservation_ids)
        response = self._client.complete(
            base_url=self._settings.base_url,
            api_key=self._settings.api_key,
            model=self._settings.model,
            messages=BuildMessages(candidates),
            timeout_seconds=self._settings.timeout_seconds,
            max_tokens=self._settings.max_tokens,
            reserve_attempt=tracker.reserve,
            maximum_attempts=1,
        )
        self._finish_attempts(tracker.reservation_ids[first_offset:], response)
        completed_at = _in_deadline_timezone(self._now(), tracker.deadline)
        parsed, error = _parse_primary_response(response, candidates, completed_at)
        if parsed is not None or response.content is None or response.attempts >= 2 or completed_at >= tracker.deadline:
            return parsed, response, error

        repair_offset = len(tracker.reservation_ids)
        repaired = self._client.complete(
            base_url=self._settings.base_url,
            api_key=self._settings.api_key,
            model=self._settings.model,
            messages=BuildRepairMessages(candidates, response.content, error),
            timeout_seconds=self._settings.timeout_seconds,
            max_tokens=self._settings.max_tokens,
            reserve_attempt=tracker.reserve,
            maximum_attempts=1,
        )
        self._finish_attempts(tracker.reservation_ids[repair_offset:], repaired)
        combined = _combine_results(response, repaired)
        parsed, repair_error = _parse_primary_response(
            repaired,
            candidates,
            _in_deadline_timezone(self._now(), tracker.deadline),
        )
        return parsed, combined, repair_error or error

    def _finish_attempts(self, reservation_ids: Sequence[str], response: DeepSeekHttpResult) -> None:
        if len(reservation_ids) != len(response.attempt_records):
            raise RuntimeError("DeepSeek reservation and attempt counts diverged")
        for reservation_id, attempt in zip(reservation_ids, response.attempt_records, strict=True):
            has_response_metadata = attempt.succeeded
            self._budget.finish(
                reservation_id,
                status="success" if attempt.succeeded else "failed",
                error=attempt.error,
                http_status=attempt.http_status,
                latency_ms=attempt.latency_ms,
                token_count=attempt.token_count,
                timed_out=attempt.timed_out,
                completed_at=self._now(),
                actual_model=response.actual_model if has_response_metadata else None,
                system_fingerprint=response.system_fingerprint if has_response_metadata else None,
                finish_reason=response.finish_reason if has_response_metadata else None,
                prompt_tokens=_usage_integer(response.usage, "prompt_tokens") if has_response_metadata else 0,
                completion_tokens=_usage_integer(response.usage, "completion_tokens") if has_response_metadata else 0,
                prompt_cache_hit_tokens=response.prompt_cache_hit_tokens if has_response_metadata else 0,
                prompt_cache_miss_tokens=response.prompt_cache_miss_tokens if has_response_metadata else 0,
            )

    def classify(self, review: DeepSeekReview, strategy: Strategy) -> DeepSeekReview:
        return ClassifyReview(
            review,
            dimension_weights=self._dimension_weights[strategy],
            confidence_coverage_min=self._confidence_coverage_min,
            minimum_known_dimensions=self._minimum_known_dimensions,
        )

    def fusion_cache_key(
        self,
        raw_key: str | CacheRequestKey,
        strategy: Strategy,
    ) -> CacheRequestKey:
        return StrategyReviewCacheKey(
            raw_key,
            strategy=strategy,
            strategy_version=self._strategy_version,
            dimension_weights=self._dimension_weights[strategy],
            confidence_coverage_min=self._confidence_coverage_min,
            minimum_known_dimensions=self._minimum_known_dimensions,
        )


def _parse_primary_response(
    response: DeepSeekHttpResult,
    candidates: Sequence[FeatureSnapshot],
    completed_at: datetime,
) -> tuple[dict[str, DeepSeekReview] | None, str]:
    if response.content is None:
        return None, response.error
    if response.finish_reason == "length":
        return None, "finish_reason_length"
    try:
        return ParseReviews(response.content, candidates, completed_at), ""
    except DeepSeekSchemaError as exc:
        return None, str(exc)
