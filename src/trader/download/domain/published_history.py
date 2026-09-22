"""Immutable identities exposed by the published history read boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trader.download.domain.history_revision import HistoryRevision


@dataclass(frozen=True, slots=True)
class PublishedHistoryManifest:
    snapshot_hash: str
    sequence: int
    data_cutoff: date
    calendar_dates: tuple[date, ...]
    universe_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        dates = tuple(self.calendar_dates)
        codes = tuple(self.universe_codes)
        if (
            len(self.snapshot_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.snapshot_hash)
            or self.sequence < 1
            or not dates
            or dates != tuple(sorted(set(dates)))
            or dates[-1] != self.data_cutoff
            or not codes
            or codes != tuple(sorted(set(codes)))
            or any(len(code) != 6 or not code.isdigit() for code in codes)
        ):
            raise ValueError("published history manifest is invalid")
        object.__setattr__(self, "calendar_dates", dates)
        object.__setattr__(self, "universe_codes", codes)


@dataclass(frozen=True, slots=True)
class PublishedHistoryWindow:
    code: str
    revisions: tuple[HistoryRevision, ...]

    def __post_init__(self) -> None:
        revisions = tuple(self.revisions)
        dates = tuple(item.trade_date for item in revisions)
        if (
            len(self.code) != 6
            or not self.code.isdigit()
            or any(item.code != self.code for item in revisions)
            or dates != tuple(sorted(set(dates)))
        ):
            raise ValueError("published history window is invalid")
        object.__setattr__(self, "revisions", revisions)


__all__ = ["PublishedHistoryManifest", "PublishedHistoryWindow"]
