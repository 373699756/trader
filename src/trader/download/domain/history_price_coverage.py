"""Actual qualified row coverage, distinct from HTTP success and training readiness."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HistoryPriceCoverage:
    expected: int
    raw: int
    qfq: int
    paired: int

    def __post_init__(self) -> None:
        if (
            any(type(value) is not int or value < 0 for value in (self.expected, self.raw, self.qfq, self.paired))
            or not self.paired <= min(self.raw, self.qfq) <= max(self.raw, self.qfq) <= self.expected
        ):
            raise ValueError("history_price_coverage_invalid")

    @property
    def missing(self) -> int:
        return self.expected - self.paired


__all__ = ["HistoryPriceCoverage"]
