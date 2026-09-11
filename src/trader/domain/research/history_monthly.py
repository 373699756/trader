"""Immutable values for monthly historical rows and bounded training windows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from trader.domain.research.artifact_identity import canonical_artifact_hash
from trader.domain.research.baostock_daily import (
    BaoStockBoard,
    BaoStockDailyCell,
    BaoStockTrainingRow,
)

HISTORY_TRAINING_WINDOW_SESSIONS = 61


@dataclass(frozen=True)
class HistoryMonthlyRevision:
    first_seen_sequence: int
    board: BaoStockBoard
    cell: BaoStockDailyCell
    is_st: bool | None
    industry: str | None
    industry_classification: str | None
    revision_id: str = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.first_seen_sequence < 1:
            raise ValueError("history monthly revision sequence is invalid")
        if self.board not in {"main", "chinext", "star"}:
            raise ValueError("history monthly revision board is invalid")
        if self.is_st is not None and not isinstance(self.is_st, bool):
            raise ValueError("history monthly revision ST fact is invalid")
        industry = self.industry.strip() if self.industry is not None else None
        classification = self.industry_classification.strip() if self.industry_classification is not None else None
        if (industry is None) != (classification is None) or industry == "" or classification == "":
            raise ValueError("history monthly revision industry facts are invalid")
        object.__setattr__(self, "industry", industry)
        object.__setattr__(self, "industry_classification", classification)
        revision_id = canonical_artifact_hash((self.board, self.cell, self.is_st, industry, classification))
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "content_hash", canonical_artifact_hash(self))

    @property
    def code(self) -> str:
        return self.cell.code

    @property
    def trade_date(self) -> date:
        return self.cell.trade_date

    @property
    def training_row(self) -> BaoStockTrainingRow | None:
        if (
            not self.cell.obtained
            or self.cell.unadjusted is None
            or self.cell.qfq is None
            or self.is_st is None
            or self.industry is None
        ):
            return None
        return BaoStockTrainingRow(
            self.code,
            self.trade_date,
            self.board,
            self.industry,
            self.is_st,
            self.cell.unadjusted,
            self.cell.qfq,
        )


@dataclass(frozen=True)
class HistoryTrainingWindow:
    rows: tuple[BaoStockTrainingRow, ...]

    def __post_init__(self) -> None:
        rows = tuple(self.rows)
        if len(rows) != HISTORY_TRAINING_WINDOW_SESSIONS:
            raise ValueError("history training window must contain exactly 61 sessions")
        if len({row.code for row in rows}) != 1:
            raise ValueError("history training window must contain one code")
        dates = tuple(row.trade_date for row in rows)
        if dates != tuple(sorted(set(dates))):
            raise ValueError("history training window dates are invalid")
        object.__setattr__(self, "rows", rows)

    @property
    def code(self) -> str:
        return self.rows[-1].code

    @property
    def trade_date(self) -> date:
        return self.rows[-1].trade_date


__all__ = [
    "HISTORY_TRAINING_WINDOW_SESSIONS",
    "HistoryMonthlyRevision",
    "HistoryTrainingWindow",
]
