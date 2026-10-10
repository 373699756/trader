"""Lazy deterministic fact ordering used only when supplier coordinates tie."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property, total_ordering

from trader.infra.market_data.observations import JsonScalar, SourceObservation


def scalar_order(value: JsonScalar) -> tuple[int, str | float]:
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, float(value))
    if isinstance(value, float):
        return (2, value)
    return (3, value)


@total_ordering
@dataclass(frozen=True, eq=False)
class ObservationFacts:
    observation: SourceObservation

    @cached_property
    def key(self) -> tuple[tuple[str, tuple[int, str | float]], ...]:
        return tuple((name, scalar_order(value)) for name, value in sorted(self.observation.fields.items()))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ObservationFacts):
            return NotImplemented
        return self.observation.fields == other.observation.fields

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ObservationFacts):
            return NotImplemented
        return self.key < other.key
