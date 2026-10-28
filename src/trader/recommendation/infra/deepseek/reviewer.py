"""DeepSeek review orchestration with persisted states, cache and budgets."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from trader.recommendation.infra.runtime.composition_inputs import RuntimeBuildContext

from trader.infra.cache_contracts import CacheRequestKey
from trader.infra.settings import DeepSeekSettings
from trader.recommendation.application.ports.deepseek import DeepSeekReviewUnavailableError
from trader.recommendation.application.ports.json_values import JsonObject
from trader.recommendation.domain.evidence.review import (
    DeepSeekReview,
    ReviewCandidateContext,
    ReviewOutcome,
)
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.recommendation.infra.deepseek.budget_batch_ledger import BudgetBatchCompletion, BudgetBatchRequest
from trader.recommendation.infra.deepseek.cache import ReviewCache
from trader.recommendation.infra.deepseek.completion_client_contract import DeepSeekCompletionClient, DeepSeekHttpResult
from trader.recommendation.infra.deepseek.reviewer_context import ReviewerContext
from trader.recommendation.infra.deepseek.reviewer_requests import ReviewerRequestExecutor
from trader.recommendation.infra.deepseek.reviewer_selection import (
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
from trader.recommendation.infra.deepseek.reviewer_status import ReviewerStatusTracker
from trader.recommendation.infra.deepseek.schema import (
    RAW_FACTS_CACHE_GENERATION,
    BuildReviewManifestHash,
    ReviewCacheKey,
)

_SUCCESSFUL_CANDIDATE_OUTCOMES = frozenset({ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN})


class _ReviewerRequiredOptions(TypedDict):
    dimension_weights: Mapping[Strategy, Mapping[str, float]]
    strategy_version: str
    confidence_coverage_min: float
    minimum_known_dimensions: int


class _ReviewerOptionalOptions(TypedDict, total=False):
    profile: str
    now: Callable[[], datetime]
    monotonic: Callable[[], float]


class ReviewerOptions(_ReviewerRequiredOptions, _ReviewerOptionalOptions):
    pass


class _ReviewRequiredOptions(TypedDict):
    phase: str
    deadline: datetime


class _ReviewOptionalOptions(TypedDict, total=False):
    budget_bucket: str | None
    emergency_reason: str
    contexts: Mapping[str, ReviewCandidateContext] | None


class _ReviewOptions(_ReviewRequiredOptions, _ReviewOptionalOptions):
    pass


class _ExecuteReviewOptions(TypedDict):
    phase: str
    deadline: datetime
    now: datetime
    planned_bucket: str
    emergency_reason: str
    batch_id: str
    contexts: Mapping[str, ReviewCandidateContext]
    monotonic_deadline: float


@dataclass(frozen=True)
class _ReviewExecution:
    strategy: Strategy
    candidates: Sequence[FeatureSnapshot]
    phase: str
    deadline: datetime
    now: datetime
    planned_bucket: str
    emergency_reason: str
    batch_id: str
    contexts: Mapping[str, ReviewCandidateContext]
    thinking_mode: str
    requested_model: str
    monotonic_deadline: float
    claimed_raw_keys: set[CacheRequestKey] = field(default_factory=set)


@dataclass(frozen=True)
class _TerminalState:
    outcome: ReviewOutcome
    completed_at: datetime
    error: str


@dataclass(frozen=True)
class _ParsedBatch:
    execution: _ReviewExecution
    candidates: Sequence[FeatureSnapshot]
    parsed: Mapping[str, DeepSeekReview]
    response: DeepSeekHttpResult
    completed_at: datetime
    results: dict[str, DeepSeekReview]


class DeepSeekReviewer:
    @classmethod
    def from_context(
        cls,
        context: RuntimeBuildContext,
        budget: DeepSeekBudgetLedger,
        client: DeepSeekCompletionClient,
        cache: ReviewCache,
    ) -> DeepSeekReviewer:
        """Parse policy dependencies while retaining explicitly injected resource owners."""
        strategy = context.strategy
        return cls(
            context.settings.deepseek,
            budget,
            client,
            cache,
            dimension_weights={Strategy(name): weights for name, weights in strategy.dimension_weights.items()},
            strategy_version=strategy.strategy_version,
            confidence_coverage_min=strategy.fusion.confidence_coverage_min,
            minimum_known_dimensions=strategy.fusion.minimum_known_dimensions,
            now=context.now,
            profile=strategy.scoring_profile,
        )

    def __init__(
        self,
        settings: DeepSeekSettings,
        budget: DeepSeekBudgetLedger,
        client: DeepSeekCompletionClient,
        cache: ReviewCache,
        **options: Unpack[ReviewerOptions],
    ) -> None:
        dimension_weights = options["dimension_weights"]
        strategy_version = options["strategy_version"]
        confidence_coverage_min = options["confidence_coverage_min"]
        minimum_known_dimensions = options["minimum_known_dimensions"]
        now = options.get("now", lambda: datetime.now(timezone.utc))
        self._monotonic = options.get("monotonic", time.monotonic)
        self._settings = settings
        self._budget = budget
        self._client = client
        self._cache = cache
        self._dimension_weights = {strategy: dict(weights) for strategy, weights in dimension_weights.items()}
        self._strategy_version = strategy_version
        self._confidence_coverage_min = confidence_coverage_min
        self._minimum_known_dimensions = minimum_known_dimensions
        self._now = now
        self._profile = options.get("profile", "v2")
        context = ReviewerContext(
            settings=settings,
            budget=budget,
            client=client,
            cache=cache,
            dimension_weights=self._dimension_weights,
            strategy_version=strategy_version,
            confidence_coverage_min=confidence_coverage_min,
            minimum_known_dimensions=minimum_known_dimensions,
            now=now,
        )
        self._status = ReviewerStatusTracker(context)
        self._requests = ReviewerRequestExecutor(context, self._status)

    def review(
        self,
        strategy: Strategy,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
        contexts: Mapping[str, ReviewCandidateContext] | None = None,
    ) -> Mapping[str, DeepSeekReview]:
        try:
            return self._review(strategy, candidates, phase=phase, deadline=deadline, contexts=contexts)
        except DeepSeekReviewUnavailableError:
            raise
        except Exception as exc:
            raise DeepSeekReviewUnavailableError("DeepSeek review boundary is unavailable") from exc

    def preheat(
        self,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
    ) -> Mapping[str, DeepSeekReview]:
        return self._review(
            Strategy.TOMORROW,
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
        **options: Unpack[_ReviewOptions],
    ) -> Mapping[str, DeepSeekReview]:
        phase = options["phase"]
        deadline = options["deadline"]
        budget_bucket = options.get("budget_bucket")
        emergency_reason = options.get("emergency_reason", "")
        contexts = options.get("contexts")
        now = _in_deadline_timezone(self._now(), deadline)
        monotonic_deadline = self._monotonic() + max(0.0, (deadline - now).total_seconds())
        unique_candidates = _unique_candidates(candidates)
        if strategy is Strategy.LONG:
            self._status.start(strategy, phase, 0)
            return {}
        planned_bucket = budget_bucket or strategy.value
        self._status.start(strategy, phase, len(unique_candidates))
        batch_id = self._budget.begin_batch(
            BudgetBatchRequest(
                profile=self._profile,
                strategy=strategy,
                phase=phase,
                bucket=planned_bucket,
                model=self._settings.model,
                requested_at=now,
                deadline=deadline,
                candidate_codes=tuple(candidate.quote.code for candidate in unique_candidates),
            )
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
                monotonic_deadline=monotonic_deadline,
            )
        except Exception as exc:
            error = f"internal_{exc.__class__.__name__}"
            completed_at = _in_deadline_timezone(self._now(), deadline)
            self._budget.fail_running_batch(batch_id, completed_at=completed_at, error=error)
            self._status.record_internal_failure(error)
            raise

    def _execute_review(
        self,
        strategy: Strategy,
        unique_candidates: Sequence[FeatureSnapshot],
        **options: Unpack[_ExecuteReviewOptions],
    ) -> Mapping[str, DeepSeekReview]:
        phase = options["phase"]
        deadline = options["deadline"]
        now = options["now"]
        planned_bucket = options["planned_bucket"]
        emergency_reason = options["emergency_reason"]
        batch_id = options["batch_id"]
        contexts = options["contexts"]
        execution = _ReviewExecution(
            strategy,
            unique_candidates,
            phase,
            deadline,
            now,
            planned_bucket,
            emergency_reason,
            batch_id,
            contexts,
            _thinking_mode(self._client, self._settings.model),
            self._settings.model,
            options["monotonic_deadline"],
        )
        early = self._early_review_result(execution)
        if early is not None:
            return early
        try:
            results, missing, waiting = self._restore_review_cache(execution)
            slice_statuses, physical_attempts, last_error = self._run_primary_batches(execution, missing, results)
            # All owned requests finish before waiting for another owner. This
            # prevents crossed candidate orders from holding each other's claims.
            self._restore_waiting_reviews(execution, waiting, results)
            cache_hits = sum(review.cache_reused for review in results.values())
            self._budget.set_batch_cache_hits(batch_id, cache_hits)
            self._status.set_cache_hits(cache_hits)
            batch_status = _aggregate_batch_status(slice_statuses, cache_hits=cache_hits)
            self._status.finish_batch(
                BudgetBatchCompletion(
                    batch_id,
                    batch_status,
                    _in_deadline_timezone(self._now(), deadline),
                    results,
                    physical_attempts,
                    last_error,
                )
            )
            return results
        finally:
            for key in tuple(execution.claimed_raw_keys):
                self._cache.release_raw(key)
                execution.claimed_raw_keys.discard(key)

    def _early_review_result(self, execution: _ReviewExecution) -> Mapping[str, DeepSeekReview] | None:
        if not execution.candidates:
            completion = BudgetBatchCompletion(
                execution.batch_id,
                "skipped",
                execution.now,
                {},
                0,
                "no_eligible_candidates",
            )
            self._status.finish_batch(completion)
            return {}
        if not self._settings.enabled or not self._settings.api_key:
            error = "disabled" if not self._settings.enabled else "api_key_missing"
            results: dict[str, DeepSeekReview] = {}
            self._set_terminal(
                results,
                execution.candidates,
                execution,
                _TerminalState(ReviewOutcome.REJECTED, execution.now, error),
            )
            self._status.finish_batch(
                BudgetBatchCompletion(execution.batch_id, "skipped", execution.now, results, 0, error)
            )
            return results
        if execution.now >= execution.deadline:
            results = {}
            self._set_terminal(
                results,
                execution.candidates,
                execution,
                _TerminalState(ReviewOutcome.LATE, execution.now, "deadline_reached"),
            )
            self._status.finish_batch(
                BudgetBatchCompletion(
                    execution.batch_id,
                    "skipped",
                    execution.now,
                    results,
                    0,
                    "deadline_reached",
                )
            )
            return results
        return None

    def _restore_review_cache(
        self,
        execution: _ReviewExecution,
    ) -> tuple[dict[str, DeepSeekReview], list[FeatureSnapshot], list[FeatureSnapshot]]:
        results: dict[str, DeepSeekReview] = {}
        missing: list[tuple[int, int, FeatureSnapshot]] = []
        waiting: list[FeatureSnapshot] = []
        for index, candidate in enumerate(execution.candidates):
            if not _has_callable_features(candidate):
                self._set_terminal(
                    results,
                    (candidate,),
                    execution,
                    _TerminalState(ReviewOutcome.ABSTAIN, execution.now, "insufficient_structured_features"),
                )
                continue
            raw_key = self._raw_cache_key(candidate, execution)
            if self._restore_cached_review(candidate, raw_key, execution, results):
                continue
            if not self._cache.claim_raw(raw_key):
                waiting.append(candidate)
                continue
            execution.claimed_raw_keys.add(raw_key)
            # The preceding owner can publish between the first lookup and our
            # claim. Recheck before issuing a duplicate physical request.
            if self._restore_cached_review(candidate, raw_key, execution, results):
                self._release_raw_claim(raw_key, execution)
                continue
            was_seen = self._cache.has_seen(candidate.quote.code, candidate.quote.source_time.date().isoformat())
            priority = _review_priority(
                candidate, was_seen=was_seen, context=execution.contexts.get(candidate.quote.code)
            )
            missing.append((priority, index, candidate))
        ordered_missing = [item[2] for item in sorted(missing, key=lambda item: (item[0], item[1]))]
        return results, ordered_missing, waiting

    def _restore_cached_review(
        self,
        candidate: FeatureSnapshot,
        raw_key: CacheRequestKey,
        execution: _ReviewExecution,
        results: dict[str, DeepSeekReview],
    ) -> bool:
        now = _in_deadline_timezone(self._now(), execution.deadline)
        if now >= execution.deadline or self._monotonic() >= execution.monotonic_deadline:
            return False
        raw_review = self._cache.get_bound_raw(raw_key, candidate, bound_at=now)
        if raw_review is None:
            return False
        annotated = self._annotate_primary(raw_review, candidate, execution)
        fusion_key = self._requests.fusion_cache_key(raw_key, execution.strategy)
        classified = self._cache.get_fusion(fusion_key)
        if classified is None or classified.evidence_manifest_hash != raw_review.evidence_manifest_hash:
            classified = self._requests.classify(annotated, execution.strategy)
            self._cache.put_fusion(fusion_key, classified)
        results[candidate.quote.code] = replace(
            self._annotate_primary(classified, candidate, execution),
            cache_reused=True,
            evidence_binding=raw_review.evidence_binding,
        )
        return True

    def _restore_waiting_reviews(
        self,
        execution: _ReviewExecution,
        waiting: Sequence[FeatureSnapshot],
        results: dict[str, DeepSeekReview],
    ) -> None:
        for candidate in waiting:
            raw_key = self._raw_cache_key(candidate, execution)
            now = _in_deadline_timezone(self._now(), execution.deadline)
            remaining = min(
                (execution.deadline - now).total_seconds(), execution.monotonic_deadline - self._monotonic()
            )
            if remaining > 0.0:
                self._cache.wait_raw(raw_key, remaining)
            if self._restore_cached_review(candidate, raw_key, execution, results):
                continue
            completed = _in_deadline_timezone(self._now(), execution.deadline)
            late = completed >= execution.deadline or self._monotonic() >= execution.monotonic_deadline
            self._set_terminal(
                results,
                (candidate,),
                execution,
                _TerminalState(
                    ReviewOutcome.LATE if late else ReviewOutcome.REJECTED,
                    completed,
                    "deadline_reached" if late else "singleflight_upstream_unavailable",
                ),
            )

    def _run_primary_batches(
        self,
        execution: _ReviewExecution,
        missing: Sequence[FeatureSnapshot],
        results: dict[str, DeepSeekReview],
    ) -> tuple[list[str], int, str]:
        tracker = _ReservationTracker(
            budget=self._budget,
            strategy=execution.strategy,
            phase=execution.phase,
            deadline=execution.deadline,
            now=self._now,
            planned_bucket=execution.planned_bucket,
            batch_id=execution.batch_id,
            emergency_reason=execution.emergency_reason,
            model_role="primary",
            requested_model=self._settings.model,
            reasoning_effort="",
            reservation_deadline=execution.deadline,
            monotonic_deadline=execution.monotonic_deadline,
            monotonic=self._monotonic,
        )
        statuses: list[str] = []
        physical_attempts = 0
        last_error = ""
        for start in range(0, len(missing), self._settings.batch_size):
            candidate_batch = missing[start : start + self._settings.batch_size]
            tracker.candidate_count = len(candidate_batch)
            tracker.emergency_reason = execution.emergency_reason or _automatic_emergency_reason(
                candidate_batch,
                execution.phase,
            )
            status, attempts, error = self._process_primary_batch(execution, candidate_batch, tracker, results)
            statuses.append(status)
            physical_attempts += attempts
            last_error = error or last_error
        return statuses, physical_attempts, last_error

    def _process_primary_batch(
        self,
        execution: _ReviewExecution,
        candidates: Sequence[FeatureSnapshot],
        tracker: _ReservationTracker,
        results: dict[str, DeepSeekReview],
    ) -> tuple[str, int, str]:
        completed_at = _in_deadline_timezone(self._now(), execution.deadline)
        if completed_at >= execution.deadline or self._monotonic() >= execution.monotonic_deadline:
            self._set_terminal(
                results,
                candidates,
                execution,
                _TerminalState(ReviewOutcome.LATE, completed_at, "deadline_reached"),
            )
            return "skipped", 0, "deadline_reached"
        parsed, response, parse_error = self._requests.request_and_parse(candidates, tracker)
        self._status.record_attempt_status(response)
        completed_at = _in_deadline_timezone(self._now(), execution.deadline)
        if parsed is None:
            error = (
                tracker.failure_reason or parse_error or response.error
                if response.attempts == 0
                else parse_error or response.error
            )
            error = error or "request_failed"
            outcome = (
                ReviewOutcome.LATE
                if completed_at >= execution.deadline or self._monotonic() >= execution.monotonic_deadline
                else ReviewOutcome.REJECTED
            )
            terminal_error = "completed_after_deadline" if outcome is ReviewOutcome.LATE else error
            self._set_terminal(
                results,
                candidates,
                execution,
                _TerminalState(outcome, completed_at, terminal_error),
            )
            return ("failed" if response.attempts else "skipped"), response.attempts, error
        missing_result = self._apply_parsed_batch(
            _ParsedBatch(execution, candidates, parsed, response, completed_at, results)
        )
        return ("partial" if missing_result else "success"), response.attempts, ""

    def _apply_parsed_batch(self, batch: _ParsedBatch) -> bool:
        missing_result = False
        for candidate in batch.candidates:
            raw_review = batch.parsed.get(candidate.quote.code)
            if raw_review is None:
                missing_result = True
                self._set_terminal(
                    batch.results,
                    (candidate,),
                    batch.execution,
                    _TerminalState(ReviewOutcome.REJECTED, batch.completed_at, "result_missing"),
                )
                continue
            if (
                batch.completed_at >= batch.execution.deadline
                or self._monotonic() >= batch.execution.monotonic_deadline
            ):
                self._set_terminal(
                    batch.results,
                    (candidate,),
                    batch.execution,
                    _TerminalState(ReviewOutcome.LATE, batch.completed_at, "completed_after_deadline"),
                )
                continue
            parsed_review = self._annotate_primary(
                raw_review,
                candidate,
                batch.execution,
                response=batch.response,
            )
            raw_key = self._raw_cache_key(candidate, batch.execution)
            self._cache.put_raw(raw_key, candidate, parsed_review)
            self._release_raw_claim(raw_key, batch.execution)
            classified = self._requests.classify(parsed_review, batch.execution.strategy)
            self._cache.put_fusion(self._requests.fusion_cache_key(raw_key, batch.execution.strategy), classified)
            batch.results[candidate.quote.code] = self._annotate_primary(
                classified,
                candidate,
                batch.execution,
                response=batch.response,
            )
        return missing_result

    def _set_terminal(
        self,
        results: dict[str, DeepSeekReview],
        candidates: Sequence[FeatureSnapshot],
        execution: _ReviewExecution,
        state: _TerminalState,
    ) -> None:
        for candidate in candidates:
            terminal = _terminal_review(candidate, state.outcome, state.completed_at, state.error)
            results[candidate.quote.code] = self._annotate_primary(terminal, candidate, execution)
            self._release_raw_claim(self._raw_cache_key(candidate, execution), execution)

    def _release_raw_claim(self, key: CacheRequestKey, execution: _ReviewExecution) -> None:
        if key not in execution.claimed_raw_keys:
            return
        self._cache.release_raw(key)
        execution.claimed_raw_keys.remove(key)

    def _annotate_primary(
        self,
        review: DeepSeekReview,
        candidate: FeatureSnapshot,
        execution: _ReviewExecution,
        *,
        response: DeepSeekHttpResult | None = None,
    ) -> DeepSeekReview:
        return _annotate_review(
            review,
            candidate=candidate,
            review_stage="primary",
            requested_model=execution.requested_model,
            actual_model=response.actual_model if response is not None else None,
            thinking_mode=execution.thinking_mode,
            model_role="primary",
            system_fingerprint=response.system_fingerprint if response is not None else None,
            prompt_cache_hit_tokens=response.prompt_cache_hit_tokens if response is not None else None,
            prompt_cache_miss_tokens=response.prompt_cache_miss_tokens if response is not None else None,
        )

    def _raw_cache_key(self, candidate: FeatureSnapshot, execution: _ReviewExecution) -> CacheRequestKey:
        return ReviewCacheKey(
            candidate,
            model=self._settings.model,
            generation=RAW_FACTS_CACHE_GENERATION,
            model_role="primary",
            thinking_mode=execution.thinking_mode,
            reasoning_effort=None,
        )

    def status(self) -> JsonObject:
        return self._status.status()

    def evidence_manifest_hash(self, candidate: FeatureSnapshot) -> str:
        return BuildReviewManifestHash(candidate)


__all__ = ["DeepSeekReviewer"]
