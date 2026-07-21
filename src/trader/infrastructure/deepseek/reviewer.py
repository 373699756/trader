"""DeepSeek review orchestration with persisted states, cache and budgets."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone

from trader.domain.models import (
    DeepSeekReview,
    FeatureSnapshot,
    ReviewCandidateContext,
    ReviewOutcome,
    Strategy,
)
from trader.infrastructure.deepseek.base_client import DeepSeekClientBase
from trader.infrastructure.deepseek.budget import DeepSeekBudgetStore
from trader.infrastructure.deepseek.cache import ReviewCache
from trader.infrastructure.deepseek.reviewer_requests import ReviewerRequestsMixin
from trader.infrastructure.deepseek.reviewer_status import ReviewerStatusMixin
from trader.infrastructure.deepseek.reviewer_support import (
    _aggregate_batch_status,
    _annotate_review,
    _automatic_emergency_reason,
    _has_callable_features,
    _in_deadline_timezone,
    _ReservationTracker,
    _review_priority,
    _terminal_review,
    _thinking_mode,
    _unique_candidates,
)
from trader.infrastructure.deepseek.schema import (
    review_cache_key,
)
from trader.infrastructure.settings import DeepSeekSettings

_SUCCESSFUL_CANDIDATE_OUTCOMES = frozenset({ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN})


class DeepSeekReviewer(ReviewerRequestsMixin, ReviewerStatusMixin):
    def __init__(
        self,
        settings: DeepSeekSettings,
        budget: DeepSeekBudgetStore,
        client: DeepSeekClientBase,
        cache: ReviewCache,
        *,
        dimension_weights: Mapping[Strategy, Mapping[str, float]],
        strategy_version: str,
        confidence_coverage_min: float,
        minimum_known_dimensions: int,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._settings = settings
        self._budget = budget
        self._client = client
        self._cache = cache
        self._dimension_weights = {strategy: dict(weights) for strategy, weights in dimension_weights.items()}
        self._strategy_version = strategy_version
        self._confidence_coverage_min = confidence_coverage_min
        self._minimum_known_dimensions = minimum_known_dimensions
        self._now = now
        self._last_error = ""
        self._last_batch_status = "idle"
        self._last_candidate_count = 0
        self._last_candidate_outcomes: dict[str, int] = {}
        self._last_phase = ""
        self._last_strategy = ""
        self._last_cache_hits = 0
        self._last_physical_attempts = 0
        self._last_successful_attempts = 0
        self._last_failed_attempts = 0
        self._status_lock = threading.Lock()

    def review(
        self,
        strategy: Strategy,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
        contexts: Mapping[str, ReviewCandidateContext] | None = None,
    ) -> Mapping[str, DeepSeekReview]:
        return self._review(strategy, candidates, phase=phase, deadline=deadline, contexts=contexts)

    def preheat(
        self,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
    ) -> Mapping[str, DeepSeekReview]:
        return self._review(
            Strategy.TODAY,
            candidates,
            phase=phase,
            deadline=deadline,
            budget_bucket="shared_preheat",
        )

    def review_emergency(
        self,
        strategy: Strategy,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
        reason: str,
    ) -> Mapping[str, DeepSeekReview]:
        return self._review(
            strategy,
            candidates,
            phase=phase,
            deadline=deadline,
            budget_bucket="emergency",
            emergency_reason=reason,
        )

    def _review(
        self,
        strategy: Strategy,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
        budget_bucket: str | None = None,
        emergency_reason: str = "",
        contexts: Mapping[str, ReviewCandidateContext] | None = None,
    ) -> Mapping[str, DeepSeekReview]:
        now = _in_deadline_timezone(self._now(), deadline)
        unique_candidates = _unique_candidates(candidates)
        planned_bucket = budget_bucket or strategy.value
        self._start_status(strategy, phase, len(unique_candidates))
        batch_id = self._budget.begin_batch(
            strategy,
            phase=phase,
            bucket=planned_bucket,
            model=self._settings.model,
            requested_at=now,
            deadline=deadline,
            candidate_codes=tuple(candidate.quote.code for candidate in unique_candidates),
        )
        try:
            return self._execute_review(
                strategy,
                unique_candidates,
                phase=phase,
                deadline=deadline,
                now=now,
                planned_bucket=planned_bucket,
                emergency_reason=emergency_reason,
                batch_id=batch_id,
                contexts=contexts or {},
            )
        except Exception as exc:
            error = f"internal_{exc.__class__.__name__}"
            completed_at = _in_deadline_timezone(self._now(), deadline)
            self._budget.fail_running_batch(batch_id, completed_at=completed_at, error=error)
            with self._status_lock:
                self._last_batch_status = "failed"
                self._last_error = error
            raise

    def _execute_review(
        self,
        strategy: Strategy,
        unique_candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
        now: datetime,
        planned_bucket: str,
        emergency_reason: str,
        batch_id: str,
        contexts: Mapping[str, ReviewCandidateContext],
    ) -> Mapping[str, DeepSeekReview]:
        thinking_mode = _thinking_mode(self._client, self._settings.model)
        requested_model = self._settings.model
        if not unique_candidates:
            self._finish_batch(batch_id, "skipped", now, {}, 0, "no_eligible_candidates")
            return {}

        if not self._settings.enabled or not self._settings.api_key:
            error = "disabled" if not self._settings.enabled else "api_key_missing"
            rejected = {
                candidate.quote.code: _annotate_review(
                    _terminal_review(candidate, ReviewOutcome.REJECTED, now, error),
                    candidate=candidate,
                    review_stage="primary",
                    challenger_status="not_run",
                    requested_model=requested_model,
                    actual_model=None,
                    thinking_mode=thinking_mode,
                )
                for candidate in unique_candidates
            }
            self._finish_batch(batch_id, "skipped", now, rejected, 0, error)
            return rejected
        if now >= deadline:
            late = {
                candidate.quote.code: _annotate_review(
                    _terminal_review(candidate, ReviewOutcome.LATE, now, "deadline_reached"),
                    candidate=candidate,
                    review_stage="primary",
                    challenger_status="not_run",
                    requested_model=requested_model,
                    actual_model=None,
                    thinking_mode=thinking_mode,
                )
                for candidate in unique_candidates
            }
            self._finish_batch(batch_id, "skipped", now, late, 0, "deadline_reached")
            return late

        results: dict[str, DeepSeekReview] = {}
        prioritized_missing: list[tuple[int, int, FeatureSnapshot]] = []
        generation = phase
        for index, candidate in enumerate(unique_candidates):
            if not _has_callable_features(candidate):
                results[candidate.quote.code] = _annotate_review(
                    _terminal_review(
                        candidate,
                        ReviewOutcome.ABSTAIN,
                        now,
                        "insufficient_structured_features",
                    ),
                    candidate=candidate,
                    review_stage="primary",
                    challenger_status="not_run",
                    requested_model=requested_model,
                    actual_model=None,
                    thinking_mode=thinking_mode,
                )
                continue
            raw_key = review_cache_key(
                candidate,
                model=self._settings.model,
                generation=generation,
                model_role="primary",
                thinking_mode=thinking_mode,
                reasoning_effort=None,
            )
            was_seen = self._cache.has_seen(
                candidate.quote.code,
                candidate.quote.source_time.date().isoformat(),
            )
            raw_review = self._cache.get_raw(raw_key, candidate)
            if raw_review is None:
                priority = _review_priority(
                    candidate,
                    was_seen=was_seen,
                    context=contexts.get(candidate.quote.code),
                )
                prioritized_missing.append((priority, index, candidate))
                continue
            raw_review = _annotate_review(
                raw_review,
                candidate=candidate,
                review_stage="primary",
                challenger_status="not_run",
                requested_model=requested_model,
                actual_model=None,
                thinking_mode=thinking_mode,
            )
            fusion_key = self._fusion_cache_key(raw_key, strategy)
            classified = self._cache.get_fusion(fusion_key)
            if classified is None:
                classified = self._classify(raw_review, strategy)
                self._cache.put_fusion(fusion_key, classified)
            results[candidate.quote.code] = _annotate_review(
                classified,
                candidate=candidate,
                review_stage="primary",
                challenger_status="not_run",
                requested_model=requested_model,
                actual_model=None,
                thinking_mode=thinking_mode,
            )

        missing = [item[2] for item in sorted(prioritized_missing, key=lambda item: (item[0], item[1]))]

        cache_hits = sum(
            review.outcome in _SUCCESSFUL_CANDIDATE_OUTCOMES and review.error != "insufficient_structured_features"
            for review in results.values()
        )
        self._budget.set_batch_cache_hits(batch_id, cache_hits)
        self._set_cache_hits(cache_hits)
        slice_statuses: list[str] = []
        physical_attempts = 0
        last_error = ""
        automatic_emergency_reason = emergency_reason or _automatic_emergency_reason(missing, phase)
        tracker = _ReservationTracker(
            budget=self._budget,
            strategy=strategy,
            phase=phase,
            deadline=deadline,
            now=self._now,
            planned_bucket=planned_bucket,
            batch_id=batch_id,
            emergency_reason=automatic_emergency_reason,
            model_role="primary",
            requested_model=self._settings.model,
            reasoning_effort="",
        )

        for start in range(0, len(missing), self._settings.batch_size):
            candidate_batch = missing[start : start + self._settings.batch_size]
            completed_at = _in_deadline_timezone(self._now(), deadline)
            if completed_at >= deadline:
                for candidate in candidate_batch:
                    results[candidate.quote.code] = _annotate_review(
                        _terminal_review(
                            candidate,
                            ReviewOutcome.LATE,
                            completed_at,
                            "deadline_reached",
                        ),
                        candidate=candidate,
                        review_stage="primary",
                        challenger_status="not_run",
                        requested_model=requested_model,
                        actual_model=None,
                        thinking_mode=thinking_mode,
                    )
                slice_statuses.append("skipped")
                last_error = "deadline_reached"
                continue

            parsed, response, parse_error = self._request_and_parse(candidate_batch, tracker)
            physical_attempts += response.attempts
            self._record_attempt_status(response)
            completed_at = _in_deadline_timezone(self._now(), deadline)
            if parsed is None:
                error = (
                    parse_error
                    or (tracker.failure_reason if response.attempts == 0 else response.error)
                    or "request_failed"
                )
                outcome = ReviewOutcome.LATE if completed_at >= deadline else ReviewOutcome.REJECTED
                terminal_error = "completed_after_deadline" if outcome is ReviewOutcome.LATE else error
                for candidate in candidate_batch:
                    results[candidate.quote.code] = _annotate_review(
                        _terminal_review(candidate, outcome, completed_at, terminal_error),
                        candidate=candidate,
                        review_stage="primary",
                        challenger_status="not_run",
                        requested_model=requested_model,
                        actual_model=None,
                        thinking_mode=thinking_mode,
                    )
                slice_statuses.append("failed" if response.attempts else "skipped")
                last_error = error
                continue

            missing_result = False
            for candidate in candidate_batch:
                raw_review = parsed.get(candidate.quote.code)
                if raw_review is None:
                    missing_result = True
                    results[candidate.quote.code] = _annotate_review(
                        _terminal_review(
                            candidate,
                            ReviewOutcome.REJECTED,
                            completed_at,
                            "result_missing",
                        ),
                        candidate=candidate,
                        review_stage="primary",
                        challenger_status="not_run",
                        requested_model=requested_model,
                        actual_model=None,
                        thinking_mode=thinking_mode,
                    )
                    continue
                if completed_at >= deadline:
                    results[candidate.quote.code] = _annotate_review(
                        _terminal_review(candidate, ReviewOutcome.LATE, completed_at, "completed_after_deadline"),
                        candidate=candidate,
                        review_stage="primary",
                        challenger_status="not_run",
                        requested_model=requested_model,
                        actual_model=None,
                        thinking_mode=thinking_mode,
                    )
                    continue
                raw_key = review_cache_key(
                    candidate,
                    model=self._settings.model,
                    generation=generation,
                    model_role="primary",
                    thinking_mode=thinking_mode,
                    reasoning_effort=None,
                )
                parsed_review = _annotate_review(
                    raw_review,
                    candidate=candidate,
                    review_stage="primary",
                    challenger_status="not_run",
                    requested_model=requested_model,
                    actual_model=response.actual_model,
                    thinking_mode=thinking_mode,
                    model_role="primary",
                    system_fingerprint=response.system_fingerprint,
                    prompt_cache_hit_tokens=response.prompt_cache_hit_tokens,
                    prompt_cache_miss_tokens=response.prompt_cache_miss_tokens,
                )
                self._cache.put_raw(raw_key, candidate, parsed_review)
                classified = self._classify(parsed_review, strategy)
                self._cache.put_fusion(self._fusion_cache_key(raw_key, strategy), classified)
                results[candidate.quote.code] = _annotate_review(
                    classified,
                    candidate=candidate,
                    review_stage="primary",
                    challenger_status="not_run",
                    requested_model=requested_model,
                    actual_model=parsed_review.actual_model,
                    thinking_mode=thinking_mode,
                    model_role="primary",
                    system_fingerprint=parsed_review.system_fingerprint,
                    prompt_cache_hit_tokens=parsed_review.prompt_cache_hit_tokens,
                    prompt_cache_miss_tokens=parsed_review.prompt_cache_miss_tokens,
                )
            slice_statuses.append("partial" if missing_result else "success")

        challenger_attempts = self._run_challenger(
            strategy,
            unique_candidates,
            results,
            contexts=contexts,
            phase=phase,
            deadline=deadline,
            planned_bucket=planned_bucket,
            emergency_reason=automatic_emergency_reason,
            batch_id=batch_id,
        )
        physical_attempts += challenger_attempts
        batch_status = _aggregate_batch_status(slice_statuses, cache_hits=cache_hits)
        self._finish_batch(
            batch_id,
            batch_status,
            _in_deadline_timezone(self._now(), deadline),
            results,
            physical_attempts,
            last_error,
        )
        return results


__all__ = ["DeepSeekReviewer"]
