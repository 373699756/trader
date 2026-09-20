"""Immutable values for monthly historical rows and bounded training windows."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from trader.download.domain.baostock_daily import (
    BaoStockBoard,
    BaoStockDailyCell,
    BaoStockTradingStatus,
    BaoStockTrainingRow,
)
from trader.training.domain.evaluation.artifact_identity import canonical_artifact_hash

HISTORY_TRAINING_WINDOW_SESSIONS = 61
MAX_HISTORY_TRAINING_WINDOW_SESSIONS = 251


@dataclass(frozen=True)
class HistoryRevision:
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

    @property
    def training_point(self) -> HistoryTrainingPoint | None:
        """Project a revision to the compact facts retained by rolling training windows."""

        if (
            not self.cell.obtained
            or self.cell.unadjusted is None
            or self.cell.qfq is None
            or self.is_st is None
            or self.industry is None
        ):
            return None
        return HistoryTrainingPoint(
            self.trade_date,
            self.cell.qfq.close_price,
            self.cell.qfq.amount,
        )


@dataclass(frozen=True, slots=True)
class HistoryTrainingPoint:
    """The only per-session facts required by profile feature construction."""

    trade_date: date
    qfq_close_price: float | None
    qfq_amount: float | None

    def __post_init__(self) -> None:
        if type(self.trade_date) is not date:
            raise ValueError("history training point date is invalid")
        if any(
            value is not None and (not math.isfinite(value) or value < 0.0)
            for value in (self.qfq_close_price, self.qfq_amount)
        ):
            raise ValueError("history training point contains an invalid number")


@dataclass(frozen=True, slots=True)
class HistoryTrainingWindow:
    code: str
    board: BaoStockBoard
    industry: str
    is_st: bool
    trading_status: BaoStockTradingStatus
    points: tuple[HistoryTrainingPoint, ...]
    required_sessions: int = HISTORY_TRAINING_WINDOW_SESSIONS

    def __post_init__(self) -> None:
        points = tuple(self.points)
        if not HISTORY_TRAINING_WINDOW_SESSIONS <= self.required_sessions <= MAX_HISTORY_TRAINING_WINDOW_SESSIONS:
            raise ValueError("history training window session count is invalid")
        if len(points) != self.required_sessions:
            raise ValueError(
                f"history training window does not match its required {self.required_sessions} session count"
            )
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("history training window code is invalid")
        if self.board not in {"main", "chinext", "star"} or self.trading_status not in {"trading", "suspended"}:
            raise ValueError("history training window market facts are invalid")
        if not self.industry.strip() or not isinstance(self.is_st, bool):
            raise ValueError("history training window security facts are invalid")
        dates = tuple(point.trade_date for point in points)
        if dates != tuple(sorted(set(dates))):
            raise ValueError("history training window dates are invalid")
        object.__setattr__(self, "industry", self.industry.strip())
        object.__setattr__(self, "points", points)

    @property
    def trade_date(self) -> date:
        return self.points[-1].trade_date


__all__ = [
    "HISTORY_TRAINING_WINDOW_SESSIONS",
    "MAX_HISTORY_TRAINING_WINDOW_SESSIONS",
    "HistoryRevision",
    "HistoryTrainingPoint",
    "HistoryTrainingWindow",
]
