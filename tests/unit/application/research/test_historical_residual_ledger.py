from dataclasses import replace
from datetime import date

from trader.application.research.historical_residual_ledger import HistoricalResidualLedgerService
from trader.domain.research.historical_residual_ledger import (
    HistoricalOutcomeRecord,
    HistoricalPredictionRecord,
    ResidualJoinKey,
    join_prediction_outcome,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64


class _LedgerPort:
    def __init__(self) -> None:
        self.predictions = ()
        self.outcomes = ()

    def append_predictions(self, records):
        self.predictions += records

    def append_outcomes(self, records):
        self.outcomes += records

    def read_joined(self, strategy, parent_split_hash):
        outcomes = {
            (item.key, item.parent_split_hash): item for item in self.outcomes if item.label_status == "matured"
        }
        return tuple(
            join_prediction_outcome(item, outcome)
            for item in self.predictions
            if item.key.strategy == strategy
            and item.parent_split_hash == parent_split_hash
            and (outcome := outcomes.get((item.key, item.parent_split_hash))) is not None
        )

    def read_predictions(self, strategy, parent_split_hash):
        return tuple(
            item
            for item in self.predictions
            if item.key.strategy == strategy and item.parent_split_hash == parent_split_hash
        )


def _prediction():
    return HistoricalPredictionRecord(
        ResidualJoinKey("tomorrow", date(2024, 1, 2), "14:50", "600001", 1),
        _HASH_A,
        _HASH_A,
        _HASH_B,
        "main",
        "industrial",
        "normal",
        "liquid",
        "normal",
        0.01,
        80.0,
        "passed",
        True,
        "top6",
    )


def _pending():
    return HistoricalOutcomeRecord(
        _prediction().key,
        _HASH_A,
        None,
        None,
        None,
        None,
        None,
        None,
        "label_pending",
    )


def test_service_preserves_pending_labels_then_summarizes_only_matured_exact_rows() -> None:
    port = _LedgerPort()
    service = HistoricalResidualLedgerService(port)

    pending = service.append("tomorrow", _HASH_A, (_prediction(),), (_pending(),))
    matured_outcome = HistoricalOutcomeRecord(
        _prediction().key,
        _HASH_A,
        0.018,
        0.001,
        0.002,
        0.015,
        -0.3,
        False,
        "matured",
    )
    ready = service.append("tomorrow", _HASH_A, (), (matured_outcome,))
    second_prediction = replace(
        _prediction(),
        key=replace(_prediction().key, code="600002"),
        feature_hash=_HASH_B,
    )
    partial = service.append(
        "tomorrow",
        _HASH_A,
        (second_prediction,),
        (replace(_pending(), key=second_prediction.key),),
    )

    assert pending.status == "label_pending"
    assert pending.summary is None
    assert ready.status == "residuals_ready"
    assert ready.summary is not None
    assert ready.summary.evaluated_rows == 1
    assert partial.status == "label_pending"
    assert partial.summary is None
    assert ready.terminal_holdout_opened is False
    assert ready.production_authority is False
