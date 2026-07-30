from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader.application.ports.data_plane import (
    DataPlaneConflictError,
    DataPlaneRecoverySummary,
    HistoricalFeatureRecord,
    RiskEvidenceRecord,
    SecurityMasterRecord,
    SourceCursorRecord,
)
from trader.application.ports.types import JsonObject
from trader.infra.persistence import data_plane_sqlite
from trader.infra.persistence.data_plane import DataPlaneRepository

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_recent_records_for_all_families_round_trip(tmp_path: Path) -> None:
    repository = DataPlaneRepository(tmp_path)
    repository.initialize()

    repository.save_security_master_recent(_security_master_record("600001", payload={"field": "sm"}))
    repository.save_historical_feature_recent(_historical_feature_record("600001", payload={"field": "hf"}))
    repository.save_risk_evidence_recent(_risk_evidence_record("600001", "r1"))
    repository.save_source_cursor_recent(_source_cursor_record("cursor-1"))

    assert repository.load_security_master_recent("600001") == _security_master_record(
        "600001", payload={"field": "sm"}
    )
    assert repository.load_historical_feature_recent("600001", "2026-07-30") == _historical_feature_record(
        "600001", payload={"field": "hf"}
    )
    assert repository.load_risk_evidence_recent("600001", "r1") == _risk_evidence_record("600001", "r1")
    assert repository.load_source_cursor_recent("cursor-1") == _source_cursor_record("cursor-1")


def test_formal_records_are_idempotent_for_same_payload_and_conflict_on_different_payload(tmp_path: Path) -> None:
    repository = DataPlaneRepository(tmp_path)
    repository.initialize()
    record = _security_master_record("600002", payload={"field": "formal"})

    repository.save_security_master_formal("freeze-2026-07-30", record)
    repository.save_security_master_formal("freeze-2026-07-30", record)

    with pytest.raises(DataPlaneConflictError, match="formal save conflicts"):
        repository.save_security_master_formal(
            "freeze-2026-07-30", _security_master_record("600002", payload={"field": "changed"})
        )


def test_load_verification_failure_quarantines_committed_row_and_returns_none(tmp_path: Path) -> None:
    repository = DataPlaneRepository(tmp_path)
    record = _security_master_record("600003", payload={"field": "valid"})
    repository.save_security_master_recent(record)
    db = _database_path(tmp_path)

    with data_plane_sqlite.connection_scope(db) as connection:
        connection.execute(
            "UPDATE security_master_recent SET payload = ?, payload_hash = ? WHERE code = ?",
            ('{"invalid": true', "000000000000000000000000000000000000000000000000000000000000000000", "600003"),
        )

    assert repository.load_security_master_recent("600003") is None

    with data_plane_sqlite.connection_scope(db) as connection:
        row = connection.execute(
            "SELECT status, error FROM security_master_recent WHERE code = ?",
            ("600003",),
        ).fetchone()
        audit_count = connection.execute(
            "SELECT COUNT(*) AS count FROM data_plane_quarantine_audit WHERE record_identity = 'code=600003'"
        ).fetchone()

    assert row is not None
    assert str(row["status"]) == "quarantined"
    assert str(row["error"]) == "load_verification_failed"
    assert audit_count is not None
    assert int(audit_count["count"]) == 1


def test_recovery_promotes_staged_rows_and_quarantines_corrupted_committed_rows(tmp_path: Path) -> None:
    repository = DataPlaneRepository(tmp_path)
    repository.initialize()
    _insert_staged_source_cursor_row(
        _database_path(tmp_path),
        cursor_name="cursor-staged",
        payload={"cursor": "ok"},
    )
    repository.save_security_master_recent(_security_master_record("600004", payload={"field": "to-corrupt"}))

    with data_plane_sqlite.connection_scope(_database_path(tmp_path)) as connection:
        connection.execute(
            "UPDATE security_master_recent SET payload = ? WHERE code = ?",
            ('{"invalid": true', "600004"),
        )

    summary = repository.recover()

    assert summary == DataPlaneRecoverySummary(
        recovered=1,
        quarantined=1,
        orphaned=1,
    )
    assert repository.load_source_cursor_recent("cursor-staged") == _source_cursor_record(
        "cursor-staged", payload={"cursor": "ok"}
    )
    assert repository.load_security_master_recent("600004") is None


def _insert_staged_source_cursor_row(database: Path, *, cursor_name: str, payload: JsonObject) -> None:
    text = _canonical_json(payload)
    payload_bytes = text.encode()
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    with data_plane_sqlite.connection_scope(database) as connection:
        connection.execute(
            """
            INSERT INTO source_cursor_recent(
                cursor_name, observed_at, source_time, source, data_version, schema_version,
                payload_hash, payload, cursor_value, status, error, recovery_payload, recovery_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cursor_name,
                _timestamp(9, 30),
                _timestamp(9, 29),
                "unit",
                "v1",
                "v2_data_plane_v1",
                payload_hash,
                text,
                "value-1",
                "staged",
                "",
                payload_bytes,
                payload_hash,
            ),
        )


def _security_master_record(
    code: str,
    *,
    payload: JsonObject | None = None,
) -> SecurityMasterRecord:
    payload_data = payload or {"code": code}
    return SecurityMasterRecord(
        code=code,
        observed_at=_timestamp(9, 30),
        source_time=_timestamp(9, 29),
        source="unit",
        data_version="v1",
        payload=payload_data,
        payload_hash=_payload_hash(payload_data),
        schema_version="v2_data_plane_v1",
    )


def _historical_feature_record(
    code: str,
    *,
    trade_date: str = "2026-07-30",
    payload: JsonObject | None = None,
) -> HistoricalFeatureRecord:
    payload_data = payload or {"code": code, "trade_date": trade_date}
    return HistoricalFeatureRecord(
        code=code,
        trade_date=trade_date,
        observed_at=_timestamp(9, 30),
        source_time=_timestamp(9, 29),
        source="unit",
        data_version="v1",
        payload=payload_data,
        payload_hash=_payload_hash(payload_data),
        schema_version="v2_data_plane_v1",
    )


def _risk_evidence_record(code: str, evidence_id: str) -> RiskEvidenceRecord:
    payload_data = {"code": code, "evidence_id": evidence_id}
    return RiskEvidenceRecord(
        code=code,
        evidence_id=evidence_id,
        observed_at=_timestamp(9, 30),
        source_time=_timestamp(9, 29),
        source="unit",
        data_version="v1",
        payload=payload_data,
        payload_hash=_payload_hash(payload_data),
        schema_version="v2_data_plane_v1",
    )


def _source_cursor_record(cursor_name: str, *, payload: JsonObject | None = None) -> SourceCursorRecord:
    payload_data = payload or {}
    return SourceCursorRecord(
        cursor_name=cursor_name,
        cursor_value="value-1",
        observed_at=_timestamp(9, 30),
        source_time=_timestamp(9, 29),
        source="unit",
        data_version="v1",
        payload=payload_data,
        payload_hash=_payload_hash(payload_data),
        schema_version="v2_data_plane_v1",
    )


def _timestamp(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 30, hour, minute, tzinfo=SHANGHAI)


def _canonical_json(payload: JsonObject) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _payload_hash(payload: JsonObject) -> str:
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _database_path(runtime_root: Path) -> Path:
    return runtime_root / "v2-data" / "v2-data.sqlite3"
