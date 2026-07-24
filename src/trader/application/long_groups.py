"""Typed long-watchlist group metadata for delivery."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from trader.domain.recommendation.models import Recommendation


@dataclass(frozen=True)
class LongWatchItemDefinition:
    code: str
    name: str
    industry: str


@dataclass(frozen=True)
class LongGroupDefinition:
    name: str
    category: str
    codes: tuple[str, ...]
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "codes", tuple(self.codes))


def long_groups_metadata(
    groups: Sequence[LongGroupDefinition],
    selected: Sequence[Recommendation],
) -> tuple[dict[str, object], ...]:
    selected_codes = {item.features.quote.code for item in selected}
    metadata: list[dict[str, object]] = []
    for group in groups:
        visible_codes = tuple(code for code in group.codes if code in selected_codes)
        if not visible_codes:
            continue
        metadata.append(
            {
                "name": group.name,
                "category": group.category,
                "codes": list(visible_codes),
                "count": len(visible_codes),
                "source": group.source,
            }
        )
    return tuple(metadata)


__all__ = ["LongGroupDefinition", "LongWatchItemDefinition", "long_groups_metadata"]
