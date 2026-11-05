"""Monotonic, task-local limits for optional Tencent price reviews."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from threading import Lock

from trader.download.domain.price_review import (
    PriceReviewBudgetExhausted,
    PriceReviewConfiguration,
    PriceReviewExhaustionReason,
)

_DEFAULT_REVIEW_CONFIGURATION = PriceReviewConfiguration()


class TencentRequestBudget:
    """Bound only additional reviews; ordinary downloads do not construct a budget."""

    def __init__(
        self,
        configuration: PriceReviewConfiguration = _DEFAULT_REVIEW_CONFIGURATION,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._configuration = configuration
        self._monotonic = monotonic
        self._deadline: float | None = None
        self._remaining = configuration.maximum_seconds
        self._codes: list[str] = []
        self._request_count = 0
        self._exhausted_reason: PriceReviewExhaustionReason | None = None
        self._lock = Lock()

    @property
    def reviewed_codes(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._codes)

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._request_count

    @property
    def exhausted_reason(self) -> PriceReviewExhaustionReason | None:
        with self._lock:
            return self._exhausted_reason

    def reserve_code(self, code: str) -> bool:
        if not code:
            raise ValueError("price_review_code_invalid")
        with self._lock:
            if self._remaining_seconds() <= 0:
                self._exhausted_reason = "deadline"
                return False
            if code in self._codes:
                return True
            if self._request_count >= self._configuration.maximum_requests:
                self._exhausted_reason = "request_limit"
                return False
            if len(self._codes) >= self._configuration.maximum_codes:
                self._exhausted_reason = "code_limit"
                return False
            self._codes.append(code)
            return True

    def reserve_request(self, configured_timeout_seconds: float) -> float:
        if not math.isfinite(configured_timeout_seconds) or configured_timeout_seconds <= 0:
            raise ValueError("price_review_timeout_invalid")
        with self._lock:
            remaining = self._remaining_seconds()
            if remaining <= 0:
                self._exhausted_reason = "deadline"
                raise PriceReviewBudgetExhausted("deadline")
            if self._request_count >= self._configuration.maximum_requests:
                self._exhausted_reason = "request_limit"
                raise PriceReviewBudgetExhausted("request_limit")
            self._request_count += 1
            return min(configured_timeout_seconds, remaining)

    def check_deadline(self) -> None:
        with self._lock:
            if self._remaining_seconds() <= 0:
                self._exhausted_reason = "deadline"
                raise PriceReviewBudgetExhausted("deadline")

    def pause(self) -> None:
        """Exclude ordinary sync work between review segments without refilling limits."""
        with self._lock:
            if self._deadline is not None:
                self._remaining = max(0.0, self._deadline - self._monotonic())
                self._deadline = None
                if self._remaining <= 0:
                    self._exhausted_reason = "deadline"

    def _remaining_seconds(self) -> float:
        observed = self._monotonic()
        if self._deadline is None:
            self._deadline = observed + self._remaining
        return self._deadline - observed


__all__ = ["TencentRequestBudget"]
