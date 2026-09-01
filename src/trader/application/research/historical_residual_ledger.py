"""Application owner for the append-only historical residual ledger."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from trader.application.research.replay_models import canonical_hash
from trader.domain.research.h1_point_in_time import H1Strategy
from trader.domain.research.historical_residual_ledger import (
    HistoricalOutcomeRecord,
    HistoricalPredictionRecord,
    HistoricalResidualSummary,
    JoinedHistoricalResidual,
    summarize_residuals,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HistoricalResidualLedgerPort(Protocol):
    def append_predictions(self, records: tuple[HistoricalPredictionRecord, ...]) -> None: ...

    def append_outcomes(self, records: tuple[HistoricalOutcomeRecord, ...]) -> None: ...

    def read_joined(self, strategy: H1Strategy, parent_split_hash: str) -> tuple[JoinedHistoricalResidual, ...]: ...

    def read_predictions(
        self, strategy: H1Strategy, parent_split_hash: str
    ) -> tuple[HistoricalPredictionRecord, ...]: ...


@dataclass(frozen=True)
class HistoricalResidualLedgerBatch:
    strategy: H1Strategy
    parent_split_hash: str
    status: Literal["label_pending", "residuals_ready"]
    prediction_records_received: int
    outcome_records_received: int
    summary: HistoricalResidualSummary | None
    schema_version: str = "historical_prediction_residual_ledger_batch_v1"
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.strategy not in {"today", "tomorrow", "d25"} or _SHA256.fullmatch(self.parent_split_hash) is None:
            raise ValueError("historical residual batch identity is invalid")
        if min(self.prediction_records_received, self.outcome_records_received) < 0:
            raise ValueError("historical residual batch counts are invalid")
        expected = "residuals_ready" if self.summary is not None else "label_pending"
        if self.status != expected:
            raise ValueError("historical residual batch status is inconsistent")
        if self.summary is not None and self.summary.parent_split_hash != self.parent_split_hash:
            raise ValueError("historical residual batch summary parent does not match")
        if self.schema_version != "historical_prediction_residual_ledger_batch_v1":
            raise ValueError("historical residual batch schema is invalid")
        if self.terminal_holdout_opened or self.production_authority:
            raise ValueError("historical residual batch cannot open holdout or production")
        object.__setattr__(self, "content_hash", canonical_hash(self))


class HistoricalResidualLedgerService:
    def __init__(self, ledger: HistoricalResidualLedgerPort) -> None:
        self._ledger = ledger

    def append(
        self,
        strategy: H1Strategy,
        parent_split_hash: str,
        predictions: tuple[HistoricalPredictionRecord, ...],
        outcomes: tuple[HistoricalOutcomeRecord, ...],
    ) -> HistoricalResidualLedgerBatch:
        if any(item.key.strategy != strategy or item.parent_split_hash != parent_split_hash for item in predictions):
            raise ValueError("historical residual prediction batch parent does not match")
        if any(item.key.strategy != strategy or item.parent_split_hash != parent_split_hash for item in outcomes):
            raise ValueError("historical residual outcome batch parent does not match")
        self._ledger.append_predictions(predictions)
        self._ledger.append_outcomes(outcomes)
        joined = self._ledger.read_joined(strategy, parent_split_hash)
        modeled = tuple(
            item
            for item in self._ledger.read_predictions(strategy, parent_split_hash)
            if item.predicted_net_excess_return is not None
        )
        complete = bool(modeled) and len(joined) == len(modeled)
        summary = summarize_residuals(joined) if complete else None
        return HistoricalResidualLedgerBatch(
            strategy,
            parent_split_hash,
            "residuals_ready" if summary is not None else "label_pending",
            len(predictions),
            len(outcomes),
            summary,
        )


__all__ = [
    "HistoricalResidualLedgerBatch",
    "HistoricalResidualLedgerPort",
    "HistoricalResidualLedgerService",
]
