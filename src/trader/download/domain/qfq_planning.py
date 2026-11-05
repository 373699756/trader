"""Typed local price coverage and provenance used by the market planner."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide
from trader.download.domain.qfq_window import QfqWindowSnapshot

QfqNonTradingKind = Literal["confirmed", "inferred", "legacy_unverified"]


@dataclass(frozen=True, slots=True)
class QfqNonTradingFact:
    code: str
    trade_date: date
    kind: QfqNonTradingKind
    checked_on: date | None
    source: str | None

    def __post_init__(self) -> None:
        if (
            len(self.code) != 6 or not self.code.isascii() or not self.code.isdigit()
            or type(self.trade_date) is not date
            or self.kind not in ("confirmed", "inferred", "legacy_unverified")
            or (self.kind == "legacy_unverified" and (self.checked_on is not None or self.source is not None))
            or (self.kind != "legacy_unverified" and (
                type(self.checked_on) is not date or not self.source or self.checked_on < self.trade_date
            ))
        ):
            raise ValueError("qfq_nontrading_fact_invalid")

    def cell(self) -> PublishedHistoryCell:
        if self.kind == "legacy_unverified":
            raise ValueError("qfq_nontrading_fact_unverified")
        if self.kind == "inferred":
            return PublishedHistoryCell(self.code, self.trade_date, "tencent_inferred_suspended", None, None)
        raw = PublishedHistorySide(
            self.code, self.trade_date, "unadjusted", None, None, None, None,
            None, None, None, None, None, "suspended",
        )
        adjusted = PublishedHistorySide(
            self.code, self.trade_date, "qfq", None, None, None, None,
            None, None, None, None, None, "suspended",
        )
        return PublishedHistoryCell(self.code, self.trade_date, "supplier_marked_suspended", raw, adjusted)


@dataclass(frozen=True, slots=True)
class QfqPlanningState:
    code: str
    source: str
    completed_day: date | None
    completed_source: str | None
    tencent_checked_on: date | None
    nontrading: tuple[QfqNonTradingFact, ...]


@dataclass(frozen=True, slots=True)
class QfqPlanningSnapshot:
    windows: QfqWindowSnapshot
    metadata: tuple[QfqPlanningState, ...]

    def __post_init__(self) -> None:
        codes = tuple(state.code for state in self.metadata)
        if codes != tuple(state.code for state in self.windows.states):
            raise ValueError("qfq planning metadata must match ordered window codes")

    def state_for(self, code: str) -> QfqPlanningState | None:
        index = bisect_left(self.metadata, code, key=lambda state: state.code)
        return self.metadata[index] if index < len(self.metadata) and self.metadata[index].code == code else None


__all__ = ["QfqNonTradingFact", "QfqNonTradingKind", "QfqPlanningSnapshot", "QfqPlanningState"]
