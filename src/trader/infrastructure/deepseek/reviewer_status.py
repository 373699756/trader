"""Thread-safe DeepSeek reviewer status and batch finalization mixin."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from trader.domain.models import (
    DeepSeekReview,
    ReviewOutcome,
    Strategy,
)
from trader.infrastructure.deepseek.reviewer_state import ReviewerState
from trader.infrastructure.deepseek.reviewer_support import _physical_call_acceptance

_SUCCESSFUL_CANDIDATE_OUTCOMES = frozenset({ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN})


class ReviewerStatusMixin(ReviewerState):
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
