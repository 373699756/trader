"""One bounded collection of immutable source inputs for all admitted profiles."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from trader.recommendation.domain.market.models import MarketQuote
from trader.recommendation.domain.market.refresh import IntradayRefreshResult

T = TypeVar("T")


class RoundInputCancelled(RuntimeError):
    """The source cut cannot be used after a failed or expired collection."""


class RoundCollection(Generic[T]):
    def __init__(self, profiles: tuple[str, ...], remaining: Callable[[], float]) -> None:
        self._profiles = frozenset(profiles)
        self._remaining = remaining
        self._condition = threading.Condition()
        self._requests: dict[str, tuple[str, ...]] = {}
        self._result: T | None = None
        self._failed = False

    def cancel(self) -> None:
        with self._condition:
            self._failed = True
            self._result = None
            self._condition.notify_all()

    def collect(self, profile: str, codes: tuple[str, ...], load: Callable[[tuple[str, ...]], T]) -> T:
        with self._condition:
            if profile not in self._profiles or profile in self._requests:
                raise ValueError("round_input_profile_invalid")
            self._check()
            self._requests[profile] = codes
            leader = len(self._requests) == len(self._profiles)
            union = tuple(sorted({code for request in self._requests.values() for code in request})) if leader else ()
        if leader:
            try:
                result = load(union)
            except BaseException:
                self.cancel()
                raise
            with self._condition:
                self._check()
                self._result = result
                self._condition.notify_all()
        with self._condition:
            while self._result is None:
                self._check()
                self._condition.wait(min(0.05, self._remaining()))
            self._check()
            return self._result

    def _check(self) -> None:
        if self._failed or self._remaining() <= 0:
            raise RoundInputCancelled("round_input_cancelled")


class RoundInputs:
    def __init__(self, profiles: tuple[str, ...], remaining: Callable[[], float]) -> None:
        self._remaining = remaining
        self.market = RoundCollection[tuple[MarketQuote, ...]](profiles, remaining)
        self.candidates = RoundCollection[tuple[MarketQuote, ...]](profiles, remaining)
        self.minutes = RoundCollection[IntradayRefreshResult](profiles, remaining)

    def cancelled(self) -> bool:
        return self._remaining() <= 0


@dataclass(frozen=True)
class ProfileRoundInputs:
    profile: str
    inputs: RoundInputs


__all__ = ["ProfileRoundInputs", "RoundCollection", "RoundInputCancelled", "RoundInputs"]
