"""DeepSeek primary/challenger HTTP execution and repair mixin."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import Strategy
from trader.domain.review.models import (
    DeepSeekReview,
    ReviewCandidateContext,
    ReviewOutcome,
)
from trader.infra.deepseek.base_client import DeepSeekHttpResult
from trader.infra.deepseek.challenger import (
    CHALLENGER_PROMPT_VERSION,
    CHALLENGER_SCHEMA_VERSION,
    ChallengerReview,
    build_challenger_messages,
    build_challenger_repair_messages,
    merge_challenger_review,
    parse_challenger_reviews,
)
from trader.infra.deepseek.reviewer_context import ReviewerContext
from trader.infra.deepseek.reviewer_status import ReviewerStatusTracker
from trader.infra.deepseek.reviewer_support import (
    _automatic_emergency_reason,
    _challenger_deadline,
    _challenger_failure_status,
    _combine_results,
    _in_deadline_timezone,
    _mark_challenger_unavailable,
    _ReservationTracker,
    _select_challenger_candidates,
    _thinking_mode,
    _usage_integer,
)
from trader.infra.deepseek.schema import (
    RAW_FACTS_CACHE_GENERATION,
    DeepSeekSchemaError,
    build_messages,
    build_repair_messages,
    classify_review,
    parse_reviews,
    review_cache_key,
    strategy_review_cache_key,
)

_SUCCESSFUL_CANDIDATE_OUTCOMES = frozenset({ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN})


class _ChallengerOptions(TypedDict):
    contexts: Mapping[str, ReviewCandidateContext]
    phase: str
    deadline: datetime
    planned_bucket: str
    emergency_reason: str
    batch_id: str


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

    def run_challenger(
        self,
        strategy: Strategy,
        candidates: Sequence[FeatureSnapshot],
        results: dict[str, DeepSeekReview],
        **options: Unpack[_ChallengerOptions],
    ) -> int:
        contexts = options["contexts"]
        phase = options["phase"]
        deadline = options["deadline"]
        planned_bucket = options["planned_bucket"]
        emergency_reason = options["emergency_reason"]
        batch_id = options["batch_id"]
        call_limit = self._settings.challenger_limits.get(strategy.value, 0)
        if call_limit <= 0 or planned_bucket == "shared_preheat":
            return 0
        self._restore_cached_challenger(strategy, candidates, results, phase=phase)
        selected = _select_challenger_candidates(candidates, results, contexts)
        challenger_batch_size = min(4, self._settings.batch_size)
        selected = selected[: call_limit * challenger_batch_size]
        if not selected:
            return 0
        challenger_deadline = _challenger_deadline(strategy, deadline)
        tracker = _ReservationTracker(
            budget=self._budget,
            strategy=strategy,
            phase=phase,
            deadline=challenger_deadline,
            now=self._now,
            planned_bucket=planned_bucket,
            batch_id=batch_id,
            emergency_reason=emergency_reason,
            model_role="challenger",
            requested_model=self._settings.challenger_model,
            reasoning_effort="high",
        )
        attempts = 0
        for start in range(0, len(selected), challenger_batch_size):
            candidate_batch = selected[start : start + challenger_batch_size]
            tracker.emergency_reason = emergency_reason or _automatic_emergency_reason(candidate_batch, phase)
            completed_at = _in_deadline_timezone(self._now(), challenger_deadline)
            if completed_at >= challenger_deadline:
                _mark_challenger_unavailable(results, candidate_batch, "late")
                continue
            primary = {candidate.quote.code: results[candidate.quote.code] for candidate in candidate_batch}
            parsed, response, error = self._request_challenger(candidate_batch, primary, tracker)
            attempts += response.attempts
            self._status.record_attempt_status(response)
            completed_at = _in_deadline_timezone(self._now(), challenger_deadline)
            if parsed is None:
                status = _challenger_failure_status(error, tracker.failure_reason, completed_at, challenger_deadline)
                _mark_challenger_unavailable(results, candidate_batch, status)
                continue
            for candidate in candidate_batch:
                challenge = parsed.get(candidate.quote.code)
                if challenge is None:
                    results[candidate.quote.code] = replace(
                        results[candidate.quote.code],
                        challenger_status="result_missing",
                    )
                    continue
                merged = merge_challenger_review(results[candidate.quote.code], challenge, candidate)
                classified = self.classify(
                    replace(
                        merged,
                        challenger_requested_model=self._settings.challenger_model,
                        challenger_actual_model=response.actual_model,
                        challenger_thinking_mode="reasoning",
                        challenger_reasoning_effort="high",
                        challenger_system_fingerprint=response.system_fingerprint,
                        challenger_prompt_cache_hit_tokens=response.prompt_cache_hit_tokens,
                        challenger_prompt_cache_miss_tokens=response.prompt_cache_miss_tokens,
                    ),
                    strategy,
                )
                results[candidate.quote.code] = classified
                self._cache.put_fusion(
                    self._challenger_fusion_cache_key(candidate, strategy, phase=phase),
                    classified,
                )
        return attempts

    def _restore_cached_challenger(
        self,
        strategy: Strategy,
        candidates: Sequence[FeatureSnapshot],
        results: dict[str, DeepSeekReview],
        *,
        phase: str,
    ) -> None:
        for candidate in candidates:
            review = results.get(candidate.quote.code)
            if review is None or review.outcome not in _SUCCESSFUL_CANDIDATE_OUTCOMES:
                continue
            cached = self._cache.get_fusion(self._challenger_fusion_cache_key(candidate, strategy, phase=phase))
            if cached is not None and cached.completed_at == review.completed_at:
                results[candidate.quote.code] = cached

    def _challenger_fusion_cache_key(
        self,
        candidate: FeatureSnapshot,
        strategy: Strategy,
        *,
        phase: str,
    ) -> str:
        primary_key = review_cache_key(
            candidate,
            model=self._settings.model,
            generation=RAW_FACTS_CACHE_GENERATION,
            model_role="primary",
            thinking_mode=_thinking_mode(self._client, self._settings.model),
            reasoning_effort=None,
        )
        challenger_identity = review_cache_key(
            candidate,
            model=self._settings.challenger_model,
            generation=phase,
            model_role="challenger",
            thinking_mode=_thinking_mode(self._client, self._settings.challenger_model),
            reasoning_effort="high",
            schema_version=CHALLENGER_SCHEMA_VERSION,
            prompt_version=CHALLENGER_PROMPT_VERSION,
        )
        return self.fusion_cache_key(
            primary_key,
            strategy,
            challenger_identity=challenger_identity,
            challenger_status="applied",
        )

    def _request_challenger(
        self,
        candidates: Sequence[FeatureSnapshot],
        primary_reviews: Mapping[str, DeepSeekReview],
        tracker: _ReservationTracker,
    ) -> tuple[dict[str, ChallengerReview] | None, DeepSeekHttpResult, str]:
        first_offset = len(tracker.reservation_ids)
        response = self._client.complete(
            base_url=self._settings.base_url,
            api_key=self._settings.api_key,
            model=self._settings.challenger_model,
            messages=build_challenger_messages(candidates, primary_reviews),
            timeout_seconds=self._settings.timeout_seconds,
            max_tokens=self._settings.max_tokens,
            reserve_attempt=tracker.reserve,
            maximum_attempts=2,
        )
        self._finish_attempts(tracker.reservation_ids[first_offset:], response)
        completed_at = _in_deadline_timezone(self._now(), tracker.deadline)
        parsed, parse_error = _parse_challenger_response(response, candidates, completed_at)
        if parsed is not None or response.content is None or response.attempts >= 2 or completed_at >= tracker.deadline:
            return parsed, response, parse_error
        repair_offset = len(tracker.reservation_ids)
        repaired = self._client.complete(
            base_url=self._settings.base_url,
            api_key=self._settings.api_key,
            model=self._settings.challenger_model,
            messages=build_challenger_repair_messages(
                candidates,
                primary_reviews,
                response.content,
                parse_error,
                response.reasoning_content,
            ),
            timeout_seconds=self._settings.timeout_seconds,
            max_tokens=self._settings.max_tokens,
            reserve_attempt=tracker.reserve,
            maximum_attempts=1,
        )
        self._finish_attempts(tracker.reservation_ids[repair_offset:], repaired)
        combined = _combine_results(response, repaired)
        parsed, repair_error = _parse_challenger_response(
            repaired,
            candidates,
            _in_deadline_timezone(self._now(), tracker.deadline),
        )
        return parsed, combined, repair_error or parse_error

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
            messages=build_messages(candidates),
            timeout_seconds=self._settings.timeout_seconds,
            max_tokens=self._settings.max_tokens,
            reserve_attempt=tracker.reserve,
            maximum_attempts=2,
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
            messages=build_repair_messages(candidates, response.content, error),
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
        return classify_review(
            review,
            dimension_weights=self._dimension_weights[strategy],
            confidence_coverage_min=self._confidence_coverage_min,
            minimum_known_dimensions=self._minimum_known_dimensions,
        )

    def fusion_cache_key(
        self,
        raw_key: str,
        strategy: Strategy,
        *,
        challenger_identity: str = "",
        challenger_status: str = "not_run",
    ) -> str:
        return strategy_review_cache_key(
            raw_key,
            strategy=strategy,
            strategy_version=self._strategy_version,
            dimension_weights=self._dimension_weights[strategy],
            confidence_coverage_min=self._confidence_coverage_min,
            minimum_known_dimensions=self._minimum_known_dimensions,
            challenger_identity=challenger_identity,
            challenger_status=challenger_status,
        )


def _parse_challenger_response(
    response: DeepSeekHttpResult,
    candidates: Sequence[FeatureSnapshot],
    completed_at: datetime,
) -> tuple[dict[str, ChallengerReview] | None, str]:
    if response.content is None:
        return None, response.error
    if response.finish_reason == "length":
        return None, "finish_reason_length"
    try:
        return parse_challenger_reviews(response.content, candidates, completed_at), ""
    except ValueError as exc:
        return None, str(exc)


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
        return parse_reviews(response.content, candidates, completed_at), ""
    except DeepSeekSchemaError as exc:
        return None, str(exc)
