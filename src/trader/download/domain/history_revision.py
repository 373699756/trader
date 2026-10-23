"""Immutable values for monthly historical rows and bounded training windows."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from trader.download.domain.baostock_daily import (
    BaoStockBoard,
    BaoStockCellStatus,
    BaoStockDailyCell,
    BaoStockTrainingRow,
)
from trader.infra.serialization.canonical_codec import CanonicalDigest

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
        revision_id = CanonicalDigest((self.board, self.cell))
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "content_hash", CanonicalDigest((self.first_seen_sequence, self.board, self.cell)))

    @property
    def code(self) -> str:
        return self.cell.code

    @property
    def trade_date(self) -> date:
        return self.cell.trade_date

    def can_upgrade_observation(self, previous_status: BaoStockCellStatus, pending_sequence: int | None) -> bool:
        """Only an unpublished sequence may gain facts or complete a partial price pair."""
        can_complete = self.cell.status == "complete" and previous_status in {
            "unknown_missing",
            "qfq_missing",
            "unadjusted_missing",
            "tencent_inferred_suspended",
        }
        can_gain_fact = previous_status == "unknown_missing" and self.cell.status != "unknown_missing"
        return self.first_seen_sequence == pending_sequence and (can_complete or can_gain_fact)

    def retains_partial_prices(self, previous: BaoStockDailyCell) -> bool:
        """Completing a partial observation must preserve every already verified side."""
        return (previous.unadjusted is None or previous.unadjusted == self.cell.unadjusted) and (
            previous.qfq is None or previous.qfq == self.cell.qfq
        )

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


@dataclass(frozen=True, slots=True)
class HistoryTrainingPoint:
    """The only per-session facts required by profile feature construction."""

    trade_date: date
    qfq_close_price: float | None
    qfq_amount: float | None
    qfq_open: float | None = None
    qfq_high: float | None = None
    qfq_low: float | None = None
    raw_volume: float | None = None

    def __post_init__(self) -> None:
        if type(self.trade_date) is not date:
            raise ValueError("history training point date is invalid")
        if any(
            value is not None and (not math.isfinite(value) or value < 0.0)
            for value in (
                self.qfq_close_price,
                self.qfq_amount,
                self.qfq_open,
                self.qfq_high,
                self.qfq_low,
                self.raw_volume,
            )
        ):
            raise ValueError("history training point contains an invalid number")


@dataclass(frozen=True, slots=True)
class HistoryTrainingRow:
    """One date/code observation for the training-owned rolling state."""

    code: str
    trade_date: date
    board: BaoStockBoard
    industry: str | None
    point: HistoryTrainingPoint | None
    cell_status: BaoStockCellStatus | None = None

    def __post_init__(self) -> None:
        industry = self.industry.strip() if self.industry is not None else None
        if (
            len(self.code) != 6
            or not self.code.isdigit()
            or type(self.trade_date) is not date
            or self.board not in {"main", "chinext", "star"}
            or industry == ""
            or (self.point is not None and self.point.trade_date != self.trade_date)
            or self.cell_status
            not in {
                None,
                "complete",
                "supplier_marked_suspended",
                "tencent_inferred_suspended",
                "unadjusted_missing",
                "qfq_missing",
                "unknown_missing",
            }
        ):
            raise ValueError("history training row is invalid")
        object.__setattr__(self, "industry", industry)


__all__ = [
    "MAX_HISTORY_TRAINING_WINDOW_SESSIONS",
    "HistoryRevision",
    "HistoryTrainingPoint",
    "HistoryTrainingRow",
]
