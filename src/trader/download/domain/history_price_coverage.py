"""Actual qualified row coverage, distinct from HTTP success and training readiness."""

from dataclasses import dataclass


@dataclass(frozen=True)
class HistoryPriceCoverage:
    expected: int
    raw: int
    qfq: int
    paired: int
    suspended: int = 0
    calendar_sessions: int | None = None
    not_applicable: int = 0
    inferred_suspended: int = 0

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not int or value < 0
                for value in (
                    self.expected,
                    self.raw,
                    self.qfq,
                    self.paired,
                    self.suspended,
                    self.not_applicable,
                    self.inferred_suspended,
                )
            )
            or not self.paired <= min(self.raw, self.qfq) <= max(self.raw, self.qfq) <= self.expected
            or max(self.raw, self.qfq) + self.suspended > self.expected
            or self.inferred_suspended > self.suspended
            or (
                self.calendar_sessions is not None
                and (type(self.calendar_sessions) is not int or self.calendar_sessions < self.not_applicable)
            )
        ):
            raise ValueError("history_price_coverage_invalid")

    @property
    def missing(self) -> int:
        return self.expected - self.paired - self.suspended


__all__ = ["HistoryPriceCoverage"]
