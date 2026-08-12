"""Typed long-watchlist group metadata for the V2 Long projection."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LongWatchItemDefinition:
    code: str
    name: str
    industry: str


@dataclass(frozen=True)
class LongGroupSectionDefinition:
    source_section: str
    codes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "codes", tuple(self.codes))


@dataclass(frozen=True)
class LongGroupDefinition:
    name: str
    category: str
    codes: tuple[str, ...]
    source: str = ""
    source_section: str = "current_leaders"
    sections: tuple[LongGroupSectionDefinition, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "codes", tuple(self.codes))
        object.__setattr__(self, "sections", tuple(self.sections))


__all__ = ["LongGroupDefinition", "LongGroupSectionDefinition", "LongWatchItemDefinition"]
