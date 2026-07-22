"""Thread-safe DeepSeek reviewer status and batch finalization component."""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from collections.abc import Mapping
from typing import cast
from zoneinfo import ZoneInfo

from trader.application.ports.types import JsonInput, JsonObject, freeze_json_object
from trader.domain.recommendation.models import Strategy
from trader.domain.review.models import ReviewOutcome
from trader.infra.deepseek.base_client import DeepSeekHttpResult
from trader.infra.deepseek.budget_batch_store import BudgetBatchCompletion
from trader.infra.deepseek.reviewer_context import ReviewerContext
from trader.infra.deepseek.reviewer_support import _physical_call_acceptance

_SUCCESSFUL_CANDIDATE_OUTCOMES = frozenset({ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN})


class ReviewerStatusTracker:
    def __init__(self, context: ReviewerContext) -> None:
        self._settings = context.settings
        self._budget = context.budget
        self._cache = context.cache
        self._now = context.now
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

    def start(self, strategy: Strategy, phase: str, candidate_count: int) -> None:
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

    def set_cache_hits(self, count: int) -> None:
        with self._status_lock:
            self._last_cache_hits = count

    def finish_batch(self, completion: BudgetBatchCompletion) -> None:
        self._budget.finish_batch(completion)
        outcomes = Counter(review.outcome.value for review in completion.reviews.values())
        with self._status_lock:
            self._last_batch_status = completion.status
            self._last_candidate_outcomes = dict(outcomes)
            self._last_error = completion.error[:500]

    def record_attempt_status(self, response: DeepSeekHttpResult) -> None:
        with self._status_lock:
            self._last_physical_attempts += response.attempts
            self._last_successful_attempts += sum(item.succeeded for item in response.attempt_records)
            self._last_failed_attempts += sum(not item.succeeded for item in response.attempt_records)

    def record_internal_failure(self, error: str) -> None:
        with self._status_lock:
            self._last_batch_status = "failed"
            self._last_error = error[:500]

    def status(self) -> JsonObject:
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
        return freeze_json_object(
            cast(
                Mapping[str, JsonInput],
                {
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
                },
            )
        )


__all__ = ["ReviewerStatusTracker"]
