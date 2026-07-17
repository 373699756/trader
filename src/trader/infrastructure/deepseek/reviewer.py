"""DeepSeek review orchestration with cache, budget and deadline handling."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from trader.domain.models import DeepSeekReview, FeatureSnapshot, ReviewOutcome, Strategy
from trader.infrastructure.deepseek.budget import DeepSeekBudgetStore
from trader.infrastructure.deepseek.cache import ReviewCache
from trader.infrastructure.deepseek.client import DeepSeekHttpClient
from trader.infrastructure.deepseek.schema import DeepSeekSchemaError, build_messages, parse_reviews, review_cache_key
from trader.infrastructure.settings import DeepSeekSettings


class DeepSeekReviewer:
    def __init__(
        self,
        settings: DeepSeekSettings,
        budget: DeepSeekBudgetStore,
        client: DeepSeekHttpClient,
        cache: ReviewCache,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._settings = settings
        self._budget = budget
        self._client = client
        self._cache = cache
        self._now = now
        self._last_error = ""
        self._last_batch_status = "idle"
        self._last_candidate_count = 0
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
    ) -> Mapping[str, DeepSeekReview]:
        return self._review(
            strategy,
            candidates,
            phase=phase,
            deadline=deadline,
            budget_bucket="emergency",
        )

    def _review(
        self,
        strategy: Strategy,
        candidates: Sequence[FeatureSnapshot],
        *,
        phase: str,
        deadline: datetime,
        budget_bucket: str | None = None,
    ) -> Mapping[str, DeepSeekReview]:
        now = _in_deadline_timezone(self._now(), deadline)
        with self._status_lock:
            self._last_candidate_count = len(candidates)
            self._last_phase = phase
            self._last_strategy = strategy.value
            self._last_cache_hits = 0
            self._last_physical_attempts = 0
            self._last_successful_attempts = 0
            self._last_failed_attempts = 0
            self._last_error = ""
            self._last_batch_status = "running"
        if not candidates:
            with self._status_lock:
                self._last_batch_status = "skipped"
            return {}
        if not self._settings.enabled or not self._settings.api_key:
            with self._status_lock:
                self._last_batch_status = "skipped"
            return {}
        if now >= deadline:
            with self._status_lock:
                self._last_batch_status = "late"
            return {
                candidate.quote.code: _terminal_review(candidate, ReviewOutcome.LATE, now, "deadline_reached")
                for candidate in candidates
            }

        results: dict[str, DeepSeekReview] = {}
        missing: list[FeatureSnapshot] = []
        generation = "final_review" if phase == "final_review" else "regular"
        for candidate in candidates:
            key = review_cache_key(candidate, model=self._settings.model, generation=generation)
            cached = self._cache.get(key)
            if cached is None:
                missing.append(candidate)
            else:
                results[candidate.quote.code] = cached

        with self._status_lock:
            self._last_cache_hits = len(results)

        for start in range(0, len(missing), self._settings.batch_size):
            batch = missing[start : start + self._settings.batch_size]
            if _in_deadline_timezone(self._now(), deadline) >= deadline:
                for candidate in batch:
                    results[candidate.quote.code] = _terminal_review(
                        candidate,
                        ReviewOutcome.LATE,
                        _in_deadline_timezone(self._now(), deadline),
                        "deadline_reached",
                    )
                continue
            reservations: list[str] = []

            response = self._client.complete(
                base_url=self._settings.base_url,
                api_key=self._settings.api_key,
                model=self._settings.model,
                messages=build_messages(batch),
                timeout_seconds=self._settings.timeout_seconds,
                max_tokens=self._settings.max_tokens,
                reserve_attempt=_make_reserver(
                    self._budget,
                    strategy,
                    phase,
                    deadline,
                    reservations,
                    self._now,
                    budget_bucket,
                ),
            )
            with self._status_lock:
                self._last_physical_attempts += response.attempts
                self._last_successful_attempts += sum(item.succeeded for item in response.attempt_records)
                self._last_failed_attempts += sum(not item.succeeded for item in response.attempt_records)
            if len(reservations) != len(response.attempt_records):
                raise RuntimeError("DeepSeek reservation and attempt counts diverged")
            for reservation_id, attempt in zip(reservations, response.attempt_records, strict=True):
                self._budget.finish(
                    reservation_id,
                    status="success" if attempt.succeeded else "failed",
                    error=attempt.error,
                    http_status=attempt.http_status,
                    latency_ms=attempt.latency_ms,
                    token_count=attempt.token_count,
                )
            if response.content is None:
                with self._status_lock:
                    self._last_error = response.error
                completed_at = _in_deadline_timezone(self._now(), deadline)
                outcome = ReviewOutcome.LATE if completed_at >= deadline else ReviewOutcome.REJECTED
                error = "completed_after_deadline" if outcome is ReviewOutcome.LATE else response.error
                for candidate in batch:
                    results[candidate.quote.code] = _terminal_review(
                        candidate,
                        outcome,
                        completed_at,
                        error,
                    )
                continue
            completed_at = _in_deadline_timezone(self._now(), deadline)
            try:
                parsed = parse_reviews(response.content, batch, completed_at)
            except DeepSeekSchemaError as exc:
                with self._status_lock:
                    self._last_error = str(exc)
                for candidate in batch:
                    results[candidate.quote.code] = _terminal_review(
                        candidate,
                        ReviewOutcome.REJECTED,
                        completed_at,
                        str(exc),
                    )
                continue
            for candidate in batch:
                review = parsed.get(candidate.quote.code)
                if review is None:
                    review = _terminal_review(candidate, ReviewOutcome.REJECTED, completed_at, "result_missing")
                if completed_at >= deadline:
                    review = _terminal_review(candidate, ReviewOutcome.LATE, completed_at, "completed_after_deadline")
                results[candidate.quote.code] = review
                if review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN}:
                    self._cache.put(
                        review_cache_key(candidate, model=self._settings.model, generation=generation),
                        review,
                    )

        with self._status_lock:
            self._last_batch_status = (
                "success"
                if all(review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN} for review in results.values())
                else "partial"
            )
        return results

    def status(self) -> Mapping[str, object]:
        local_day = self._now().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        budget = self._budget.summary(local_day)
        with self._status_lock:
            batch_status = self._last_batch_status
            candidate_count = self._last_candidate_count
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


def _make_reserver(
    budget: DeepSeekBudgetStore,
    strategy: Strategy,
    phase: str,
    deadline: datetime,
    reservation_ids: list[str],
    now: Callable[[], datetime],
    budget_bucket: str | None,
) -> Callable[[], bool]:
    def reserve() -> bool:
        requested_at = _in_deadline_timezone(now(), deadline)
        if requested_at >= deadline:
            return False
        reservation = budget.reserve(
            strategy,
            phase=phase,
            requested_at=requested_at,
            bucket=budget_bucket,
        )
        if reservation.allowed:
            reservation_ids.append(reservation.reservation_id)
        return reservation.allowed

    return reserve


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
    elif last_error == "budget_exhausted":
        reason = "budget_exhausted"
    elif batch_status == "late":
        reason = "deadline_reached"
    else:
        reason = "no_physical_attempt_recorded"
    return {
        "applicable": applicable,
        "passed": physical_attempts > 0 if applicable else None,
        "physical_attempts_last_batch": physical_attempts,
        "zero_call_reason": reason,
    }


__all__ = ["DeepSeekReviewer"]
