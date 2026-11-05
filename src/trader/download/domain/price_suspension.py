"""Explicit full-session suspension evidence, bounded by the queried trading day."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class PriceSuspension:
    code: str
    first_day: date
    last_day: date

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit() or self.first_day > self.last_day:
            raise ValueError("price_suspension_invalid")


@dataclass(frozen=True, slots=True)
class PriceSuspensionSnapshot:
    target_date: date
    suspensions: tuple[PriceSuspension, ...]

    def __post_init__(self) -> None:
        codes = tuple(item.code for item in self.suspensions)
        if codes != tuple(sorted(set(codes))) or any(
            item.last_day > self.target_date for item in self.suspensions
        ):
            raise ValueError("price_suspension_snapshot_invalid")

    def dates_for(self, code: str, dates: tuple[date, ...]) -> frozenset[date]:
        interval = next((item for item in self.suspensions if item.code == code), None)
        if interval is None:
            return frozenset()
        return frozenset(day for day in dates if interval.first_day <= day <= interval.last_day)


__all__ = [
    "PriceSuspension",
    "PriceSuspensionSnapshot",
]
