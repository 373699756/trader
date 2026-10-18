"""Read-only metadata contract for consumers assessing historical coverage."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol


@dataclass(frozen=True)
class HistorySourceSample:
    dates: tuple[date, ...] = ()
    timestamps: tuple[datetime, ...] = ()
    response_bytes: int = 0


class HistorySourceCapability(Protocol):
    def daily_sample(self, code: str, cutoff: date, timeout: float) -> HistorySourceSample: ...

    def minute_sample(self, code: str, anchor: date, timeout: float) -> HistorySourceSample: ...


__all__ = ["HistorySourceCapability", "HistorySourceSample"]
