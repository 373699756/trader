"""Append-only SQLite adapter for historical prediction residuals."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

from trader.infra.sqlite_codec import DecodeHash, EncodeDate, EncodeHash
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    SqliteConnection,
    SqliteSchema,
    SqliteSession,
    SqliteTransaction,
)
from trader.training.domain.evaluation.h1_point_in_time import ResearchStrategy
from trader.training.domain.evaluation.historical_residual_ledger import (
    HistoricalOutcomeRecord,
    HistoricalPredictionRecord,
    JoinedHistoricalResidual,
    JoinPredictionOutcome,
    ResidualAnchor,
    ResidualFilterState,
    ResidualJoinKey,
    ResidualLabelStatus,
)


class HistoricalResidualLedgerConflictError(RuntimeError):
    """Raised when an immutable ledger identity has different content."""


class HistoricalResidualLedgerCorruptionError(RuntimeError):
    """Raised when persisted content no longer matches its sealed hash."""


@dataclass(frozen=True)
class _LedgerRow:
    table: str
    identity: str
    key: ResidualJoinKey
    parent_split_hash: str
    content_hash: str
    payload: str
    model_hash: str | None = None


class SQLiteHistoricalResidualLedger:
    def __init__(self, path: Path, *, sqlite_policy: SqliteIoPolicy) -> None:
        self._sqlite_policy = sqlite_policy
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def append_predictions(self, records: tuple[HistoricalPredictionRecord, ...]) -> None:
        with SqliteSession(self._path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            for record in records:
                self._append_row(
                    connection,
                    _LedgerRow(
                        "historical_prediction",
                        _prediction_identity(record),
                        record.key,
                        record.parent_split_hash,
                        record.content_hash,
                        _encode_prediction(record),
                        record.model_hash,
                    ),
                )

    def append_outcomes(self, records: tuple[HistoricalOutcomeRecord, ...]) -> None:
        with SqliteSession(self._path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            for record in records:
                self._append_row(
                    connection,
                    _LedgerRow(
                        "historical_outcome",
                        _outcome_identity(record),
                        record.key,
                        record.parent_split_hash,
                        record.content_hash,
                        _encode_outcome(record),
                    ),
                )

    def read_joined(self, strategy: ResearchStrategy, parent_split_hash: str) -> tuple[JoinedHistoricalResidual, ...]:
        with SqliteSession(self._path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            outcomes = connection.run(
                "SELECT content_hash, outcome_json FROM historical_outcome WHERE strategy = ? AND parent_split_hash = ?",
                (strategy, EncodeHash(parent_split_hash)),
            ).fetchall()
        outcome_by_identity: dict[str, HistoricalOutcomeRecord] = {}
        for persisted_hash, payload in outcomes:
            outcome = _decode_outcome(str(payload))
            if outcome.content_hash != DecodeHash(persisted_hash):
                raise HistoricalResidualLedgerCorruptionError("historical outcome payload hash mismatch")
            outcome_by_identity[_outcome_identity(outcome)] = outcome
        joined: list[JoinedHistoricalResidual] = []
        for prediction in self.read_predictions(strategy, parent_split_hash):
            joined_outcome = outcome_by_identity.get(_prediction_outcome_identity(prediction))
            if joined_outcome is not None and joined_outcome.label_status == "matured":
                joined.append(JoinPredictionOutcome(prediction, joined_outcome))
        return tuple(joined)

    def read_predictions(
        self, strategy: ResearchStrategy, parent_split_hash: str
    ) -> tuple[HistoricalPredictionRecord, ...]:
        with SqliteSession(self._path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            rows = connection.run(
                "SELECT content_hash, prediction_json FROM historical_prediction "
                "WHERE strategy = ? AND parent_split_hash = ? "
                "ORDER BY trade_date, code, horizon, model_hash",
                (strategy, EncodeHash(parent_split_hash)),
            ).fetchall()
        result: list[HistoricalPredictionRecord] = []
        for persisted_hash, payload in rows:
            prediction = _decode_prediction(str(payload))
            if prediction.content_hash != DecodeHash(persisted_hash):
                raise HistoricalResidualLedgerCorruptionError("historical prediction payload hash mismatch")
            result.append(prediction)
        return tuple(result)

    def _initialize(self) -> None:
        with SqliteSession(self._path, sqlite_policy=self._sqlite_policy) as connection:
            EnsureSqliteSchema(connection, _SCHEMA, allow_create=True)

    @staticmethod
    def _append_row(
        connection: SqliteConnection,
        row: _LedgerRow,
    ) -> None:
        identity_column = "prediction_id" if row.table == "historical_prediction" else "outcome_id"
        existing = connection.run(
            f"SELECT content_hash FROM {row.table} WHERE {identity_column} = ?", (row.identity,)
        ).fetchone()
        if existing is not None:
            if DecodeHash(existing[0]) != row.content_hash:
                raise HistoricalResidualLedgerConflictError(f"immutable {row.table} identity conflicts")
            return
        values: list[object] = [
            row.identity,
            row.key.strategy,
            EncodeDate(row.key.trade_date),
            row.key.code,
            row.key.horizon,
        ]
        if row.table == "historical_prediction":
            if row.model_hash is None:
                raise ValueError("historical prediction persistence requires a model hash")
            values.append(EncodeHash(row.model_hash))
        values.extend((EncodeHash(row.parent_split_hash), EncodeHash(row.content_hash), row.payload))
        placeholders = ",".join("?" for _ in values)
        connection.run(f"INSERT INTO {row.table} VALUES ({placeholders})", values)


def _base_identity(key: ResidualJoinKey, parent_split_hash: str) -> str:
    return "|".join(
        (key.strategy, key.trade_date.isoformat(), key.anchor, key.code, str(key.horizon), parent_split_hash)
    )


def _prediction_identity(record: HistoricalPredictionRecord) -> str:
    return f"{_base_identity(record.key, record.parent_split_hash)}|{record.model_hash}"


def _outcome_identity(record: HistoricalOutcomeRecord) -> str:
    return _base_identity(record.key, record.parent_split_hash)


def _prediction_outcome_identity(record: HistoricalPredictionRecord) -> str:
    return _base_identity(record.key, record.parent_split_hash)


def _encode_key(key: ResidualJoinKey) -> dict[str, object]:
    return {
        "strategy": key.strategy,
        "trade_date": key.trade_date.isoformat(),
        "anchor": key.anchor,
        "code": key.code,
        "horizon": key.horizon,
    }


def _decode_key(payload: dict[str, object]) -> ResidualJoinKey:
    _exact_fields(payload, {"strategy", "trade_date", "anchor", "code", "horizon"})
    return ResidualJoinKey(
        cast(ResearchStrategy, str(payload["strategy"])),
        date.fromisoformat(str(payload["trade_date"])),
        cast(ResidualAnchor, str(payload["anchor"])),
        str(payload["code"]),
        _integer(payload["horizon"]),
    )


def _encode_prediction(record: HistoricalPredictionRecord) -> str:
    payload = {
        "key": _encode_key(record.key),
        "parent_split_hash": record.parent_split_hash,
        "feature_hash": record.feature_hash,
        "model_hash": record.model_hash,
        "board": record.board,
        "industry": record.industry,
        "market_state": record.market_state,
        "liquidity_state": record.liquidity_state,
        "volatility_state": record.volatility_state,
        "predicted_net_excess_return": record.predicted_net_excess_return,
        "score": record.score,
        "filter_state": record.filter_state,
        "selected": record.selected,
        "selection_reason": record.selection_reason,
        "schema_version": record.schema_version,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _decode_prediction(value: str) -> HistoricalPredictionRecord:
    payload = _json_object(value)
    _exact_fields(payload, _PREDICTION_FIELDS)
    return HistoricalPredictionRecord(
        key=_decode_key(_object(payload["key"])),
        parent_split_hash=_string(payload["parent_split_hash"]),
        feature_hash=_string(payload["feature_hash"]),
        model_hash=_string(payload["model_hash"]),
        board=_string(payload["board"]),
        industry=_string(payload["industry"]),
        market_state=_string(payload["market_state"]),
        liquidity_state=_string(payload["liquidity_state"]),
        volatility_state=_string(payload["volatility_state"]),
        predicted_net_excess_return=_optional_float(payload["predicted_net_excess_return"]),
        score=_optional_float(payload["score"]),
        filter_state=cast(ResidualFilterState, _string(payload["filter_state"])),
        selected=_bool(payload["selected"]),
        selection_reason=_string(payload["selection_reason"]),
        schema_version=_string(payload["schema_version"]),
    )


def _encode_outcome(record: HistoricalOutcomeRecord) -> str:
    payload = {
        "key": _encode_key(record.key),
        "parent_split_hash": record.parent_split_hash,
        "gross_return": record.gross_return,
        "benchmark_return": record.benchmark_return,
        "round_trip_cost": record.round_trip_cost,
        "actual_net_excess_return": record.actual_net_excess_return,
        "mae_atr20": record.mae_atr20,
        "severe_loss": record.severe_loss,
        "label_status": record.label_status,
        "schema_version": record.schema_version,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _decode_outcome(value: str) -> HistoricalOutcomeRecord:
    payload = _json_object(value)
    _exact_fields(payload, _OUTCOME_FIELDS)
    return HistoricalOutcomeRecord(
        key=_decode_key(_object(payload["key"])),
        parent_split_hash=_string(payload["parent_split_hash"]),
        gross_return=_optional_float(payload["gross_return"]),
        benchmark_return=_optional_float(payload["benchmark_return"]),
        round_trip_cost=_optional_float(payload["round_trip_cost"]),
        actual_net_excess_return=_optional_float(payload["actual_net_excess_return"]),
        mae_atr20=_optional_float(payload["mae_atr20"]),
        severe_loss=_optional_bool(payload["severe_loss"]),
        label_status=cast(ResidualLabelStatus, _string(payload["label_status"])),
        schema_version=_string(payload["schema_version"]),
    )


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("historical residual integer field is invalid")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise HistoricalResidualLedgerCorruptionError("historical residual string field is invalid")
    return value


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise HistoricalResidualLedgerCorruptionError("historical residual numeric field is invalid")
    return float(value)


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise HistoricalResidualLedgerCorruptionError("historical residual boolean field is invalid")
    return value


def _optional_bool(value: object) -> bool | None:
    return None if value is None else _bool(value)


_PREDICTION_FIELDS = {
    "key",
    "parent_split_hash",
    "feature_hash",
    "model_hash",
    "board",
    "industry",
    "market_state",
    "liquidity_state",
    "volatility_state",
    "predicted_net_excess_return",
    "score",
    "filter_state",
    "selected",
    "selection_reason",
    "schema_version",
}
_OUTCOME_FIELDS = {
    "key",
    "parent_split_hash",
    "gross_return",
    "benchmark_return",
    "round_trip_cost",
    "actual_net_excess_return",
    "mae_atr20",
    "severe_loss",
    "label_status",
    "schema_version",
}


def _json_object(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HistoricalResidualLedgerCorruptionError("historical residual JSON payload is invalid") from exc
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise HistoricalResidualLedgerCorruptionError("historical residual payload must be an object")
    return cast(dict[str, object], payload)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise HistoricalResidualLedgerCorruptionError("historical residual nested payload is invalid")
    return cast(dict[str, object], value)


def _exact_fields(payload: dict[str, object], expected: set[str]) -> None:
    if set(payload) != expected:
        raise HistoricalResidualLedgerCorruptionError("historical residual payload schema is invalid")


__all__ = [
    "HistoricalResidualLedgerConflictError",
    "HistoricalResidualLedgerCorruptionError",
    "SQLiteHistoricalResidualLedger",
]


_SCHEMA = SqliteSchema(
    1,
    (
        """
        CREATE TABLE historical_prediction (
            prediction_id TEXT NOT NULL PRIMARY KEY,
            strategy TEXT NOT NULL,
            trade_date INTEGER NOT NULL,
            code TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            model_hash BLOB NOT NULL CHECK(length(model_hash)=32),
            parent_split_hash BLOB NOT NULL CHECK(length(parent_split_hash)=32),
            content_hash BLOB NOT NULL CHECK(length(content_hash)=32),
            prediction_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE historical_outcome (
            outcome_id TEXT NOT NULL PRIMARY KEY,
            strategy TEXT NOT NULL,
            trade_date INTEGER NOT NULL,
            code TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            parent_split_hash BLOB NOT NULL CHECK(length(parent_split_hash)=32),
            content_hash BLOB NOT NULL CHECK(length(content_hash)=32),
            outcome_json TEXT NOT NULL
        )
        """,
    ),
)
