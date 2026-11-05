"""Typed limits and request contract for additional price fact reviews."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Protocol

PriceReviewExhaustionReason = Literal["code_limit", "request_limit", "deadline"]


@dataclass(frozen=True)
class PriceReviewConfiguration:
    interval_sessions: int = 5
    maximum_codes: int = 8
    maximum_requests: int = 16
    maximum_seconds: float = 15.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.interval_sessions, bool)
            or not isinstance(self.interval_sessions, int)
            or self.interval_sessions < 1
            or isinstance(self.maximum_codes, bool)
            or not isinstance(self.maximum_codes, int)
            or not 1 <= self.maximum_codes <= 8
            or isinstance(self.maximum_requests, bool)
            or not isinstance(self.maximum_requests, int)
            or not 1 <= self.maximum_requests <= 16
            or isinstance(self.maximum_seconds, bool)
            or not isinstance(self.maximum_seconds, (int, float))
            or not math.isfinite(self.maximum_seconds)
            or not 0 < self.maximum_seconds <= 15.0
        ):
            raise ValueError("price_review_configuration_invalid")


class PriceReviewBudgetExhausted(RuntimeError):
    """The additional review stopped without establishing a supplier outcome."""

    def __init__(self, reason: PriceReviewExhaustionReason) -> None:
        self.reason = reason
        super().__init__("price_review_budget_exhausted")


class PriceReviewRequestBudget(Protocol):
    """Reserve each physical request, including retry and alternate-host attempts."""

    def reserve_request(self, configured_timeout_seconds: float) -> float: ...

    def check_deadline(self) -> None: ...


class PriceReviewBudget(PriceReviewRequestBudget, Protocol):
    """One task's additional review limits and observable consumption."""

    def reserve_code(self, code: str) -> bool: ...

    def pause(self) -> None: ...

    @property
    def reviewed_codes(self) -> tuple[str, ...]: ...

    @property
    def request_count(self) -> int: ...

    @property
    def exhausted_reason(self) -> PriceReviewExhaustionReason | None: ...


__all__ = [
    "PriceReviewBudget",
    "PriceReviewBudgetExhausted",
    "PriceReviewConfiguration",
    "PriceReviewExhaustionReason",
    "PriceReviewRequestBudget",
]
