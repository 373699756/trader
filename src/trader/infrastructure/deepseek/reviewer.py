"""DeepSeek review orchestration with persisted states, cache and budgets."""

from __future__ import annotations

import math
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from trader.domain.models import DeepSeekReview, FeatureSnapshot, ReviewOutcome, RiskFact, Strategy
from trader.infrastructure.deepseek.base_client import DeepSeekClientBase, DeepSeekHttpResult
from trader.infrastructure.deepseek.budget import DeepSeekBudgetStore
from trader.infrastructure.deepseek.cache import ReviewCache
from trader.infrastructure.deepseek.schema import (
    DeepSeekSchemaError,
    build_messages,
    build_repair_messages,
    build_review_manifest_hash,
    classify_review,
    parse_reviews,
    review_cache_key,
    strategy_review_cache_key,
)
from trader.infrastructure.settings import DeepSeekSettings

_SUCCESSFUL_CANDIDATE_OUTCOMES = frozenset({ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN})


class DeepSeekReviewer:
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
    ) -> Mapping[str, DeepSeekReview]:
        return self._review(strategy, candidates, phase=phase, deadline=deadline)

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
                    actual_model=requested_model,
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
                    actual_model=requested_model,
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
                    actual_model=requested_model,
                    thinking_mode=thinking_mode,
                )
                continue
            raw_key = review_cache_key(candidate, model=self._settings.model, generation=generation)
            was_seen = self._cache.has_seen(candidate.quote.code)
            raw_review = self._cache.get_raw(raw_key, candidate)
            if raw_review is None:
                priority = _review_priority(candidate, index=index, was_seen=was_seen)
                prioritized_missing.append((priority, index, candidate))
                continue
            raw_review = _annotate_review(
                raw_review,
                candidate=candidate,
                review_stage="primary",
                challenger_status="not_run",
                requested_model=requested_model,
                actual_model=requested_model,
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
                actual_model=requested_model,
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
                        actual_model=requested_model,
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
                        actual_model=requested_model,
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
                        actual_model=requested_model,
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
                        actual_model=requested_model,
                        thinking_mode=thinking_mode,
                    )
                    continue
                raw_key = review_cache_key(candidate, model=self._settings.model, generation=generation)
                parsed_review = _annotate_review(
                    raw_review,
                    candidate=candidate,
                    review_stage="primary",
                    challenger_status="not_run",
                    requested_model=requested_model,
                    actual_model=raw_review.actual_model or requested_model,
                    thinking_mode=thinking_mode,
                )
                self._cache.put_raw(raw_key, candidate, raw_review)
                classified = self._classify(parsed_review, strategy)
                self._cache.put_fusion(self._fusion_cache_key(raw_key, strategy), classified)
                results[candidate.quote.code] = _annotate_review(
                    classified,
                    candidate=candidate,
                    review_stage="primary",
                    challenger_status="not_run",
                    requested_model=requested_model,
                    actual_model=raw_review.actual_model or requested_model,
                    thinking_mode=thinking_mode,
                )
            slice_statuses.append("partial" if missing_result else "success")

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

    def _request_and_parse(
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
        if response.content is None:
            return None, response, response.error
        completed_at = _in_deadline_timezone(self._now(), tracker.deadline)
        try:
            return parse_reviews(response.content, candidates, completed_at), response, ""
        except DeepSeekSchemaError as first_error:
            if response.attempts >= 2 or completed_at >= tracker.deadline:
                return None, response, str(first_error)

            repair_offset = len(tracker.reservation_ids)
            repaired = self._client.complete(
                base_url=self._settings.base_url,
                api_key=self._settings.api_key,
                model=self._settings.model,
                messages=build_repair_messages(candidates, response.content, str(first_error)),
                timeout_seconds=self._settings.timeout_seconds,
                max_tokens=self._settings.max_tokens,
                reserve_attempt=tracker.reserve,
                maximum_attempts=1,
            )
            self._finish_attempts(tracker.reservation_ids[repair_offset:], repaired)
            combined = _combine_results(response, repaired)
            if repaired.content is None:
                return None, combined, repaired.error or str(first_error)
            try:
                return (
                    parse_reviews(
                        repaired.content,
                        candidates,
                        _in_deadline_timezone(self._now(), tracker.deadline),
                    ),
                    combined,
                    "",
                )
            except DeepSeekSchemaError as repair_error:
                return None, combined, str(repair_error)

    def _finish_attempts(self, reservation_ids: Sequence[str], response: DeepSeekHttpResult) -> None:
        if len(reservation_ids) != len(response.attempt_records):
            raise RuntimeError("DeepSeek reservation and attempt counts diverged")
        for reservation_id, attempt in zip(reservation_ids, response.attempt_records, strict=True):
            self._budget.finish(
                reservation_id,
                status="success" if attempt.succeeded else "failed",
                error=attempt.error,
                http_status=attempt.http_status,
                latency_ms=attempt.latency_ms,
                token_count=attempt.token_count,
                timed_out=attempt.timed_out,
                completed_at=self._now(),
            )

    def _record_attempt_status(self, response: DeepSeekHttpResult) -> None:
        with self._status_lock:
            self._last_physical_attempts += response.attempts
            self._last_successful_attempts += sum(item.succeeded for item in response.attempt_records)
            self._last_failed_attempts += sum(not item.succeeded for item in response.attempt_records)

    def _classify(self, review: DeepSeekReview, strategy: Strategy) -> DeepSeekReview:
        return classify_review(
            review,
            dimension_weights=self._dimension_weights[strategy],
            confidence_coverage_min=self._confidence_coverage_min,
            minimum_known_dimensions=self._minimum_known_dimensions,
        )

    def _fusion_cache_key(self, raw_key: str, strategy: Strategy) -> str:
        return strategy_review_cache_key(
            raw_key,
            strategy=strategy,
            strategy_version=self._strategy_version,
            dimension_weights=self._dimension_weights[strategy],
            confidence_coverage_min=self._confidence_coverage_min,
            minimum_known_dimensions=self._minimum_known_dimensions,
        )

    def _start_status(self, strategy: Strategy, phase: str, candidate_count: int) -> None:
        with self._status_lock:
            self._last_candidate_count = candidate_count
            self._last_candidate_outcomes = {}
            self._last_phase = phase
            self._last_strategy = strategy.value
            self._last_cache_hits = 0
            self._last_physical_attempts = 0
            self._last_successful_attempts = 0
            self._last_failed_attempts = 0
            self._last_error = ""
            self._last_batch_status = "running"

    def _set_cache_hits(self, count: int) -> None:
        with self._status_lock:
            self._last_cache_hits = count

    def _finish_batch(
        self,
        batch_id: str,
        status: str,
        completed_at: datetime,
        reviews: Mapping[str, DeepSeekReview],
        physical_attempts: int,
        error: str,
    ) -> None:
        self._budget.finish_batch(
            batch_id,
            status=status,
            completed_at=completed_at,
            reviews=reviews,
            physical_attempts=physical_attempts,
            error=error,
        )
        outcomes = Counter(review.outcome.value for review in reviews.values())
        with self._status_lock:
            self._last_batch_status = status
            self._last_candidate_outcomes = dict(outcomes)
            self._last_error = error[:500]

    def status(self) -> Mapping[str, object]:
        local_day = self._now().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        try:
            budget = self._budget.summary(local_day)
        except (OSError, sqlite3.Error):
            budget = {
                "available": False,
                "error": "budget_store_unavailable",
            }
        with self._status_lock:
            batch_status = self._last_batch_status
            candidate_count = self._last_candidate_count
            candidate_outcomes = dict(self._last_candidate_outcomes)
            phase = self._last_phase
            strategy = self._last_strategy
            last_error = self._last_error
            cache_hits = self._last_cache_hits
            physical_attempts = self._last_physical_attempts
            successful_attempts = self._last_successful_attempts
            failed_attempts = self._last_failed_attempts
        return {
            "enabled": self._settings.enabled,
            "configured": bool(self._settings.api_key),
            "last_batch_status": batch_status,
            "last_candidate_count": candidate_count,
            "last_candidate_outcomes": candidate_outcomes,
            "last_phase": phase,
            "last_strategy": strategy,
            "last_cache_hits": cache_hits,
            "last_physical_attempts": physical_attempts,
            "last_successful_attempts": successful_attempts,
            "last_failed_attempts": failed_attempts,
            "last_error": last_error,
            "cache": self._cache.status(),
            "budget": budget,
            "physical_call_acceptance": _physical_call_acceptance(
                enabled=self._settings.enabled,
                configured=bool(self._settings.api_key),
                candidate_count=candidate_count,
                cache_hits=cache_hits,
                batch_status=batch_status,
                last_error=last_error,
                physical_attempts=physical_attempts,
            ),
        }


@dataclass
class _ReservationTracker:
    budget: DeepSeekBudgetStore
    strategy: Strategy
    phase: str
    deadline: datetime
    now: Callable[[], datetime]
    planned_bucket: str
    batch_id: str
    emergency_reason: str
    reservation_ids: list[str] = field(default_factory=list)
    failure_reason: str = ""

    def reserve(self) -> bool:
        requested_at = _in_deadline_timezone(self.now(), self.deadline)
        if requested_at >= self.deadline:
            self.failure_reason = "deadline_reached"
            return False
        reservation = self.budget.reserve(
            self.strategy,
            phase=self.phase,
            requested_at=requested_at,
            bucket=self.planned_bucket,
            emergency=self.planned_bucket == "emergency",
            emergency_reason=self.emergency_reason,
            batch_id=self.batch_id,
        )
        if (
            not reservation.allowed
            and reservation.reason == "bucket_limit"
            and self.planned_bucket == self.strategy.value
            and self.emergency_reason
        ):
            reservation = self.budget.reserve(
                self.strategy,
                phase=self.phase,
                requested_at=requested_at,
                emergency=True,
                emergency_reason=self.emergency_reason,
                batch_id=self.batch_id,
            )
        self.failure_reason = reservation.reason
        if reservation.allowed:
            self.reservation_ids.append(reservation.reservation_id)
        return reservation.allowed


def _terminal_review(
    candidate: FeatureSnapshot,
    outcome: ReviewOutcome,
    completed_at: datetime,
    error: str,
) -> DeepSeekReview:
    return DeepSeekReview(
        code=candidate.quote.code,
        outcome=outcome,
        dimensions={},
        risk_facts=(),
        completed_at=completed_at,
        error=error[:500],
    )


def _annotate_review(
    review: DeepSeekReview,
    *,
    candidate: FeatureSnapshot,
    review_stage: str,
    challenger_status: str,
    requested_model: str,
    actual_model: str,
    thinking_mode: str,
) -> DeepSeekReview:
    return replace(
        review,
        review_stage=review_stage,
        challenger_status=challenger_status,
        requested_model=requested_model or None,
        actual_model=actual_model or None,
        thinking_mode=thinking_mode or None,
        evidence_manifest_hash=review.evidence_manifest_hash or build_review_manifest_hash(candidate),
    )


def _thinking_mode(client: DeepSeekClientBase, model: str) -> str:
    try:
        capabilities = client.capabilities(model)
        return "reasoning" if capabilities.requires_reasoning_roundtrip else "standard"
    except Exception:
        return "standard"


def _unique_candidates(candidates: Sequence[FeatureSnapshot]) -> tuple[FeatureSnapshot, ...]:
    unique: dict[str, FeatureSnapshot] = {}
    for candidate in candidates:
        code = candidate.quote.code
        if code in unique:
            raise ValueError(f"duplicate DeepSeek candidate code: {code}")
        unique[code] = candidate
    return tuple(unique.values())


def _has_callable_features(candidate: FeatureSnapshot) -> bool:
    if candidate.quote.price is None or candidate.quote.price <= 0 or not math.isfinite(candidate.quote.price):
        return False
    return any(raw is not None and math.isfinite(float(raw)) for raw in candidate.values.values())


def _automatic_emergency_reason(candidates: Sequence[FeatureSnapshot], phase: str) -> str:
    if any(_is_new_high_risk(fact) for candidate in candidates for fact in candidate.external_risk_facts):
        return "new_high_risk"
    if phase == "final_review":
        return "freeze_boundary_change"
    return ""


def _review_priority(candidate: FeatureSnapshot, *, index: int, was_seen: bool) -> int:
    if not was_seen:
        return 0
    if any(_is_new_high_risk(fact) for fact in candidate.external_risk_facts):
        return 1
    if index < 18:
        return 2
    return 3


def _is_new_high_risk(fact: RiskFact) -> bool:
    return fact.severity == "high" and fact.confidence >= 0.7


def _aggregate_batch_status(statuses: Sequence[str], *, cache_hits: int) -> str:
    if not statuses:
        return "skipped"
    if all(status == "success" for status in statuses):
        return "success"
    if any(status in {"success", "partial"} for status in statuses) or cache_hits > 0:
        return "partial"
    if any(status == "failed" for status in statuses):
        return "failed"
    return "skipped"


def _combine_results(first: DeepSeekHttpResult, second: DeepSeekHttpResult) -> DeepSeekHttpResult:
    return DeepSeekHttpResult(
        content=second.content,
        status_code=second.status_code,
        attempts=first.attempts + second.attempts,
        timed_out=first.timed_out or second.timed_out,
        error=second.error,
        usage=second.usage,
        attempt_records=first.attempt_records + second.attempt_records,
    )


def _in_deadline_timezone(value: datetime, deadline: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("reviewer clock must return timezone-aware datetimes")
    if deadline.tzinfo is None:
        raise ValueError("DeepSeek deadline must be timezone-aware")
    return value.astimezone(deadline.tzinfo)


def _physical_call_acceptance(
    *,
    enabled: bool,
    configured: bool,
    candidate_count: int,
    cache_hits: int,
    batch_status: str,
    last_error: str,
    physical_attempts: int,
) -> Mapping[str, object]:
    applicable = enabled and configured and candidate_count > 0 and cache_hits < candidate_count
    if physical_attempts > 0:
        reason = ""
    elif not enabled:
        reason = "disabled"
    elif not configured:
        reason = "api_key_missing"
    elif candidate_count == 0:
        reason = "no_eligible_candidates"
    elif cache_hits >= candidate_count:
        reason = "all_candidates_cached"
    elif last_error in {"budget_exhausted", "bucket_limit", "stage_limit", "daily_hard_limit"}:
        reason = last_error
    elif last_error == "deadline_reached":
        reason = "deadline_reached"
    elif batch_status == "skipped":
        reason = last_error or "batch_skipped"
    else:
        reason = "no_physical_attempt_recorded"
    return {
        "applicable": applicable,
        "passed": physical_attempts > 0 if applicable else None,
        "physical_attempts_last_batch": physical_attempts,
        "zero_call_reason": reason,
    }


__all__ = ["DeepSeekReviewer"]
