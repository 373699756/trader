import json
import sqlite3
from dataclasses import replace
from datetime import date

import pytest

from trader.domain.research.historical_residual_ledger import (
    HistoricalOutcomeRecord,
    HistoricalPredictionRecord,
    ResidualJoinKey,
)
from trader.infra.research.historical_residual_ledger import (
    HistoricalResidualLedgerConflictError,
    HistoricalResidualLedgerCorruptionError,
    SQLiteHistoricalResidualLedger,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _prediction() -> HistoricalPredictionRecord:
    return HistoricalPredictionRecord(
        ResidualJoinKey("today", date(2024, 1, 2), "11:20", "600001", 1),
        _HASH_A,
        _HASH_A,
        _HASH_B,
        "main",
        "industrial",
        "normal",
        "liquid",
        "normal",
        0.01,
        75.0,
        "passed",
        False,
        "below_top6",
    )


def _outcome() -> HistoricalOutcomeRecord:
    return HistoricalOutcomeRecord(
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


def test_sqlite_ledger_is_append_only_idempotent_and_detects_conflicts(tmp_path) -> None:
    ledger = SQLiteHistoricalResidualLedger(tmp_path / "ledger.sqlite3")
    prediction = _prediction()
    outcome = _outcome()

    ledger.append_predictions((prediction,))
    ledger.append_predictions((prediction,))
    ledger.append_outcomes((outcome,))
    ledger.append_outcomes((outcome,))

    assert ledger.read_joined("today", _HASH_A) == (ledger.read_joined("today", _HASH_A)[0],)
    assert ledger.read_joined("today", _HASH_A)[0].prediction_error == pytest.approx(0.005)
    with pytest.raises(HistoricalResidualLedgerConflictError):
        ledger.append_predictions((replace(prediction, score=74.0),))
    with pytest.raises(HistoricalResidualLedgerConflictError):
        ledger.append_outcomes((replace(outcome, gross_return=0.017, actual_net_excess_return=0.014),))


def test_sqlite_ledger_does_not_join_different_parent_split(tmp_path) -> None:
    ledger = SQLiteHistoricalResidualLedger(tmp_path / "ledger.sqlite3")
    ledger.append_predictions((_prediction(),))
    ledger.append_outcomes((replace(_outcome(), parent_split_hash=_HASH_B),))

    assert ledger.read_joined("today", _HASH_A) == ()


def test_sqlite_ledger_rejects_prediction_payload_tampering(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = SQLiteHistoricalResidualLedger(path)
    ledger.append_predictions((_prediction(),))
    ledger.append_outcomes((_outcome(),))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE historical_prediction_v1 SET content_hash = ?", ("f" * 64,))

    with pytest.raises(HistoricalResidualLedgerCorruptionError, match="prediction"):
        ledger.read_joined("today", _HASH_A)


def test_sqlite_ledger_rejects_unknown_payload_fields(tmp_path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger = SQLiteHistoricalResidualLedger(path)
    ledger.append_predictions((_prediction(),))
    with sqlite3.connect(path) as connection:
        payload = json.loads(connection.execute("SELECT payload FROM historical_prediction_v1").fetchone()[0])
        payload["unexpected"] = True
        connection.execute(
            "UPDATE historical_prediction_v1 SET payload = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        )

    with pytest.raises(HistoricalResidualLedgerCorruptionError, match="schema"):
        ledger.read_predictions("today", _HASH_A)
