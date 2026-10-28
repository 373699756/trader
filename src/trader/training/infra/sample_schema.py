"""Named sample columns and immutable feature/split identities at the SQLite boundary."""

import hashlib
import json
import re

from trader.infra.sqlite_connection import SqliteSchema
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.sample_sequences import SEQUENCE_STORAGE_CONTRACT, SampleSequenceLayout

SAMPLE_SCHEMA_VERSION = 2
SAMPLE_TARGET_COLUMNS = (
    "target_t1", "target_t2", "target_t3", "target_t4", "target_t5", "target_d25_aggregate"
)
_RESERVED_COLUMNS = frozenset((
    "trade_date", "code", "board", "industry", "avg_amount_20d", "sequence_start", *SAMPLE_TARGET_COLUMNS
))


def TrainingFeatureNames(contracts: tuple[TrainedHeadContract, ...]) -> tuple[str, ...]:
    by_position: dict[int, str] = {}
    for contract in contracts:
        for position, name in zip(contract.feature_positions, contract.feature_manifest.names, strict=True):
            if position < 0 or by_position.setdefault(position, name) != name:
                raise ValueError("training_sample_feature_contract_conflict")
    if not by_position or set(by_position) != set(range(len(by_position))):
        raise ValueError("training_sample_feature_positions_incomplete")
    return tuple(by_position[position] for position in range(len(by_position)))


def SampleFeatureHash(names: tuple[str, ...], layout: SampleSequenceLayout | None) -> bytes:
    if (
        not names or len(set(names)) != len(names) or _RESERVED_COLUMNS.intersection(names)
        or any(re.fullmatch(r"[a-z][a-z0-9_]{0,95}", name) is None for name in names)
        or layout is not None and len(names) != layout.auxiliary + layout.steps * layout.channels
    ):
        raise ValueError("training_sample_feature_names_invalid")
    payload = (
        SAMPLE_SCHEMA_VERSION, names,
        (SEQUENCE_STORAGE_CONTRACT, layout.auxiliary, layout.steps, layout.channels) if layout else None,
    )
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).digest()


def TrainingSampleSchema(names: tuple[str, ...], layout: SampleSequenceLayout | None) -> SqliteSchema:
    SampleFeatureHash(names, layout)
    columns = names[:layout.auxiliary] if layout else names
    features = ",".join(f'"{name}" REAL NOT NULL' for name in columns)
    if layout:
        features += ",sequence_start INTEGER NOT NULL CHECK(sequence_start>=0)"
    statements: tuple[str, ...] = (
        "CREATE TABLE sample_contract (singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
        "schema_version INTEGER NOT NULL CHECK(schema_version=2),"
        "feature_hash BLOB NOT NULL CHECK(length(feature_hash)=32),"
        "split_hash BLOB CHECK(split_hash IS NULL OR length(split_hash)=32))",
        f"CREATE TABLE samples (trade_date INTEGER NOT NULL,code TEXT NOT NULL,board TEXT NOT NULL,"
        f"industry TEXT NOT NULL,avg_amount_20d REAL NOT NULL,{features},"
        + ",".join(f"{name} REAL" for name in SAMPLE_TARGET_COLUMNS)
        + ",PRIMARY KEY(trade_date,code)) WITHOUT ROWID",
        "CREATE TABLE sample_split_dates (trade_date INTEGER PRIMARY KEY,split_name TEXT NOT NULL CHECK("
        "split_name IN ('training','early_stopping','calibration','validation','holdout'))) WITHOUT ROWID",
        "CREATE TABLE sample_statistics (trade_date INTEGER NOT NULL,industry TEXT NOT NULL,target TEXT NOT NULL,"
        "sample_count INTEGER NOT NULL,target_sum REAL NOT NULL,target_sum_squares REAL NOT NULL,"
        "PRIMARY KEY(trade_date,industry,target)) WITHOUT ROWID",
        "CREATE TABLE sample_day_diagnostics (trade_date INTEGER PRIMARY KEY,candidate_count INTEGER NOT NULL,"
        "missing_t1 INTEGER NOT NULL,missing_t2 INTEGER NOT NULL,missing_t3 INTEGER NOT NULL,"
        "missing_t4 INTEGER NOT NULL,missing_t5 INTEGER NOT NULL) WITHOUT ROWID",
    )
    if layout:
        statements += (
            "CREATE TABLE sample_sequence_blocks (code TEXT NOT NULL,block_id INTEGER NOT NULL,"
            "sequence_bytes BLOB NOT NULL,PRIMARY KEY(code,block_id))",
        )
    return SqliteSchema(SAMPLE_SCHEMA_VERSION, statements)


def SampleStorageId(names: tuple[str, ...], layout: SampleSequenceLayout | None) -> str:
    return f"named_samples:{SAMPLE_SCHEMA_VERSION}:" + SampleFeatureHash(names, layout).hex()


__all__ = ["SAMPLE_TARGET_COLUMNS", "TrainingFeatureNames", "SampleFeatureHash", "SampleStorageId", "TrainingSampleSchema"]
