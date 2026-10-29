"""Immutable unresolved daily price observations."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PriceIssue:
    code: str
    dates: tuple[date, ...]
    reason: str
