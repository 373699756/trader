"""SQLite-backed persistence for the V2 data-plane families."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import cast

from trader.application.ports.data_plane import (
    DataPlaneConflictError,
    DataPlaneRecoverySummary,
    DataPlaneUnavailableError,
    HistoricalFeatureRecord,
    RiskEvidenceRecord,
    SecurityMasterRecord,
    SourceCursorRecord,
)
from trader.application.ports.types import JsonObject
from trader.infra.persistence import data_plane_sqlite
from trader.infra.persistence.data_plane_types import (
    _DEFAULT_SCHEMA_VERSION,
    _MAX_PAYLOAD_BYTES,
    _PROFILES,
    Mode,
    Record,
    _Profile,
)


class DataPlaneRepository:
    """Persist and recover V2 data-plane records."""

    def __init__(self, runtime_root: Path) -> None:
        self._runtime_root = runtime_root / "v2-data"
        self._database = self._runtime_root / "v2-data.sqlite3"
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._lock:
            self._runtime_root.mkdir(parents=True, exist_ok=True)
            data_plane_sqlite.initialize_database(self._database)

    def save_security_master_recent(self, record: SecurityMasterRecord) -> None:
        self._save("security_master", mode="recent", record=record)

    def save_security_master_formal(self, freeze_id: str, record: SecurityMasterRecord) -> None:
        self._save("security_master", mode="formal", freeze_id=freeze_id, record=record)

    def load_security_master_recent(self, code: str) -> SecurityMasterRecord | None:
        return cast(SecurityMasterRecord | None, self._load("security_master", mode="recent", code=code))

    def load_security_master_recent_records(
        self,
        codes: Sequence[str] | None = None,
    ) -> tuple[SecurityMasterRecord, ...]:
        return cast(tuple[SecurityMasterRecord, ...], self._load_records("security_master", mode="recent", codes=codes))

    def load_security_master_formal(self, freeze_id: str, code: str) -> SecurityMasterRecord | None:
        return cast(
            SecurityMasterRecord | None,
            self._load("security_master", mode="formal", freeze_id=freeze_id, code=code),
        )

    def load_security_master_formal_records(
        self,
        freeze_id: str,
        codes: Sequence[str] | None = None,
    ) -> tuple[SecurityMasterRecord, ...]:
        return cast(
            tuple[SecurityMasterRecord, ...],
            self._load_records("security_master", mode="formal", freeze_id=freeze_id, codes=codes),
        )

    def save_historical_feature_recent(self, record: HistoricalFeatureRecord) -> None:
        self._save("historical_feature", mode="recent", record=record)

    def save_historical_feature_formal(self, freeze_id: str, record: HistoricalFeatureRecord) -> None:
        self._save("historical_feature", mode="formal", freeze_id=freeze_id, record=record)

    def load_historical_feature_recent(self, code: str, trade_date: str) -> HistoricalFeatureRecord | None:
        return cast(
            HistoricalFeatureRecord | None,
            self._load("historical_feature", mode="recent", code=code, trade_date=trade_date),
        )

    def load_historical_feature_recent_records(
        self,
        codes: Sequence[str] | None = None,
    ) -> tuple[HistoricalFeatureRecord, ...]:
        return cast(
            tuple[HistoricalFeatureRecord, ...],
            self._load_records("historical_feature", mode="recent", codes=codes),
        )

    def load_historical_feature_formal(
        self,
        freeze_id: str,
        code: str,
        trade_date: str,
    ) -> HistoricalFeatureRecord | None:
        return cast(
            HistoricalFeatureRecord | None,
            self._load("historical_feature", mode="formal", freeze_id=freeze_id, code=code, trade_date=trade_date),
        )

    def load_historical_feature_formal_records(
        self,
        freeze_id: str,
        codes: Sequence[str] | None = None,
    ) -> tuple[HistoricalFeatureRecord, ...]:
        return cast(
            tuple[HistoricalFeatureRecord, ...],
            self._load_records("historical_feature", mode="formal", freeze_id=freeze_id, codes=codes),
        )

    def save_risk_evidence_recent(self, record: RiskEvidenceRecord) -> None:
        self._save("risk_evidence", mode="recent", record=record)

    def save_risk_evidence_formal(self, freeze_id: str, record: RiskEvidenceRecord) -> None:
        self._save("risk_evidence", mode="formal", freeze_id=freeze_id, record=record)

    def load_risk_evidence_recent(self, code: str, evidence_id: str) -> RiskEvidenceRecord | None:
        return cast(
            RiskEvidenceRecord | None,
            self._load("risk_evidence", mode="recent", code=code, evidence_id=evidence_id),
        )

    def load_risk_evidence_recent_records(
        self,
        codes: Sequence[str] | None = None,
    ) -> tuple[RiskEvidenceRecord, ...]:
        return cast(tuple[RiskEvidenceRecord, ...], self._load_records("risk_evidence", mode="recent", codes=codes))

    def load_risk_evidence_formal(
        self,
        freeze_id: str,
        code: str,
        evidence_id: str,
    ) -> RiskEvidenceRecord | None:
        return cast(
            RiskEvidenceRecord | None,
            self._load(
                "risk_evidence",
                mode="formal",
                freeze_id=freeze_id,
                code=code,
                evidence_id=evidence_id,
            ),
        )

    def load_risk_evidence_formal_records(
        self,
        freeze_id: str,
        codes: Sequence[str] | None = None,
    ) -> tuple[RiskEvidenceRecord, ...]:
        return cast(
            tuple[RiskEvidenceRecord, ...],
            self._load_records("risk_evidence", mode="formal", freeze_id=freeze_id, codes=codes),
        )

    def save_source_cursor_recent(self, record: SourceCursorRecord) -> None:
        self._save("source_cursor", mode="recent", record=record)

    def save_source_cursor_formal(self, freeze_id: str, record: SourceCursorRecord) -> None:
        self._save("source_cursor", mode="formal", freeze_id=freeze_id, record=record)

    def load_source_cursor_recent(self, cursor_name: str) -> SourceCursorRecord | None:
        return cast(
            SourceCursorRecord | None,
            self._load("source_cursor", mode="recent", cursor_name=cursor_name),
        )

    def load_source_cursor_recent_records(
        self,
        cursor_names: Sequence[str] | None = None,
    ) -> tuple[SourceCursorRecord, ...]:
        return cast(
            tuple[SourceCursorRecord, ...],
            self._load_records("source_cursor", mode="recent", cursor_names=cursor_names),
        )

    def load_source_cursor_formal(self, freeze_id: str, cursor_name: str) -> SourceCursorRecord | None:
        return cast(
            SourceCursorRecord | None,
            self._load("source_cursor", mode="formal", freeze_id=freeze_id, cursor_name=cursor_name),
        )

    def load_source_cursor_formal_records(
        self,
        freeze_id: str,
        cursor_names: Sequence[str] | None = None,
    ) -> tuple[SourceCursorRecord, ...]:
        return cast(
            tuple[SourceCursorRecord, ...],
            self._load_records("source_cursor", mode="formal", freeze_id=freeze_id, cursor_names=cursor_names),
        )

    def recover(self) -> DataPlaneRecoverySummary:
        self.initialize()
        recovered = 0
        quarantined = 0
        orphaned = 0

        with self._lock:
            try:
                with data_plane_sqlite.connection_scope(self._database) as connection:
                    for profile in _PROFILES.values():
                        for table in (profile.recent_table, profile.formal_table):
                            for row in connection.execute(
                                f"SELECT * FROM {table} WHERE status = ?",
                                ("staged",),
                            ).fetchall():
                                if self._recover_staged_row(connection, table, row):
                                    recovered += 1
                                else:
                                    quarantined += 1
                            for row in connection.execute(
                                f"SELECT * FROM {table} WHERE status = ?",
                                ("committed",),
                            ).fetchall():
                                if self._is_committed_row_healthy(table=table, row=row):
                                    continue
                                if self._quarantine_row(connection, table, row, reason="verification_failed"):
                                    quarantined += 1
                                    orphaned += 1
            except sqlite3.OperationalError as exc:
                raise DataPlaneUnavailableError("data plane recovery failed") from exc

        return DataPlaneRecoverySummary(recovered=recovered, quarantined=quarantined, orphaned=orphaned)

    def _save(self, family: str, *, mode: Mode, record: Record, freeze_id: str | None = None) -> None:
        self.initialize()
        profile = _profile(family)
        table = profile.formal_table if mode == "formal" else profile.recent_table
        if mode == "formal" and not freeze_id:
            raise ValueError("formal save requires freeze_id")
        if mode != "formal" and freeze_id is not None:
            raise ValueError("recent save must not provide freeze_id")

        payload_text = _canonical_json(_to_payload_dict(record.payload))
        payload_bytes = payload_text.encode("utf-8")
        if len(payload_bytes) > _MAX_PAYLOAD_BYTES:
            raise DataPlaneUnavailableError("data plane payload exceeds maximum bytes")

        payload_hash = _sha256(payload_bytes)
        if record.payload_hash and record.payload_hash != payload_hash:
            raise ValueError("payload_hash must match canonical payload")

        row = _record_to_row(profile, record, freeze_id=freeze_id)
        pk_fields = _pk_fields(profile, mode)
        pk_values = _identity_values(profile, mode=mode, freeze_id=freeze_id, record=record)

        with self._lock:
            try:
                with data_plane_sqlite.connection_scope(self._database) as connection:
                    if mode == "formal":
                        self._check_formal_idempotency(
                            connection=connection,
                            table=table,
                            pk_fields=pk_fields,
                            pk_values=pk_values,
                            payload_hash=payload_hash,
                        )

                    row.update(
                        {
                            "observed_at": _iso_datetime(record.observed_at),
                            "source_time": _iso_datetime(record.source_time),
                            "source": record.source,
                            "data_version": record.data_version,
                            "schema_version": record.schema_version or _DEFAULT_SCHEMA_VERSION,
                            "payload_hash": payload_hash,
                            "payload": payload_text,
                            "status": "staged",
                            "error": "",
                            "recovery_payload": payload_bytes,
                            "recovery_sha256": payload_hash,
                        },
                    )

                    columns = tuple(row.keys())
                    placeholders = ", ".join("?" for _ in columns)
                    update_fields = [field for field in columns if field not in pk_fields]
                    upsert = ", ".join(f"{field}=excluded.{field}" for field in update_fields)
                    connection.execute(
                        f"""
                        INSERT INTO {table} ({", ".join(columns)})
                        VALUES ({placeholders})
                        ON CONFLICT({", ".join(pk_fields)}) DO UPDATE SET
                            {upsert}
                        """,
                        tuple(row[field] for field in columns),
                    )
                    connection.execute(
                        f"""
                        UPDATE {table}
                        SET status = 'committed', error = '', recovery_payload = NULL, recovery_sha256 = ''
                        WHERE {_where_clause(pk_fields)}
                        """,
                        pk_values,
                    )
            except sqlite3.OperationalError as exc:
                raise _map_sqlite_error(exc, operation="write") from exc

    def _load(
        self,
        family: str,
        *,
        mode: Mode,
        freeze_id: str | None = None,
        **identities: str,
    ) -> Record | None:
        self.initialize()
        if mode == "formal" and not freeze_id:
            raise ValueError("formal load requires freeze_id")
        if mode != "formal" and freeze_id is not None:
            raise ValueError("recent load must not provide freeze_id")

        profile = _profile(family)
        table = profile.formal_table if mode == "formal" else profile.recent_table
        pk_fields = _pk_fields(profile, mode)
        pk_values = _identity_values(profile, mode=mode, freeze_id=freeze_id, fields=identities)

        with self._lock, data_plane_sqlite.connection_scope(self._database) as connection:
            row = connection.execute(
                f"""
                SELECT * FROM {table}
                WHERE {_where_clause(pk_fields)} AND status = 'committed'
                """,
                pk_values,
            ).fetchone()
            if row is None:
                return None
            try:
                _assert_committed_record_integrity(table, row)
                return _row_to_record(table, row)
            except (TypeError, ValueError, KeyError):
                self._quarantine_row(connection, table, row, reason="load_verification_failed")
                return None
            except sqlite3.OperationalError as exc:
                raise DataPlaneUnavailableError("data plane load failed") from exc

    def _load_records(  # noqa: C901,PLR0912,PLR0913
        self,
        family: str,
        *,
        mode: Mode,
        freeze_id: str | None = None,
        codes: Sequence[str] | None = None,
        cursor_names: Sequence[str] | None = None,
        trade_dates: Sequence[str] | None = None,
        evidence_ids: Sequence[str] | None = None,
    ) -> tuple[Record, ...]:
        self.initialize()
        if mode == "formal" and not freeze_id:
            raise ValueError("formal load requires freeze_id")
        if mode != "formal" and freeze_id is not None:
            raise ValueError("recent load must not provide freeze_id")

        profile = _profile(family)
        table = profile.formal_table if mode == "formal" else profile.recent_table
        code_filter = tuple(sorted(set(codes))) if codes else None
        cursor_filter = tuple(sorted(set(cursor_names))) if cursor_names else None
        trade_date_filter = tuple(sorted(set(trade_dates))) if trade_dates else None
        evidence_filter = tuple(sorted(set(evidence_ids))) if evidence_ids else None

        query = f"SELECT * FROM {table} WHERE status = ?"
        params: tuple[object, ...] = ("committed",)
        if mode == "formal":
            query += " AND freeze_id = ?"
            params = ("committed", freeze_id)
        query += f" ORDER BY {', '.join(_identity_fields_for_table(table))}"

        result: list[Record] = []

        with self._lock, data_plane_sqlite.connection_scope(self._database) as connection:
            try:
                for row in connection.execute(query, params).fetchall():
                    if code_filter is not None:
                        candidate = _text(row["code"])
                        if candidate not in code_filter:
                            continue
                    if cursor_filter is not None:
                        candidate = _text(row["cursor_name"])
                        if candidate not in cursor_filter:
                            continue
                    if trade_date_filter is not None:
                        candidate = _text(row["trade_date"])
                        if candidate not in trade_date_filter:
                            continue
                    if evidence_filter is not None:
                        candidate = _text(row["evidence_id"])
                        if candidate not in evidence_filter:
                            continue
                    try:
                        _assert_committed_record_integrity(table, row)
                        result.append(_row_to_record(table, row))
                    except (TypeError, ValueError, KeyError):
                        self._quarantine_row(connection, table, row, reason="load_verification_failed")
            except sqlite3.OperationalError as exc:
                raise _map_sqlite_error(exc, operation="load") from exc

        return tuple(result)

    def _check_formal_idempotency(
        self,
        *,
        connection: sqlite3.Connection,
        table: str,
        pk_fields: tuple[str, ...],
        pk_values: tuple[str, ...],
        payload_hash: str,
    ) -> None:
        existing = connection.execute(
            f"SELECT payload_hash, status FROM {table} WHERE {_where_clause(pk_fields)}",
            pk_values,
        ).fetchone()
        if existing is None:
            return
        existing_status = str(existing["status"])
        if existing_status == "quarantined":
            return
        existing_hash = str(existing["payload_hash"])
        if existing_hash == payload_hash:
            return
        raise DataPlaneConflictError("formal save conflicts with existing committed record")

    def _recover_staged_row(self, connection: sqlite3.Connection, table: str, row: sqlite3.Row) -> bool:
        try:
            recovery_payload = row["recovery_payload"]
            if recovery_payload is None:
                raise TypeError("missing recovery payload")
            recovery_payload_bytes = bytes(recovery_payload)
            if _sha256(recovery_payload_bytes) != _text(row["recovery_sha256"]):
                raise ValueError("recovery hash mismatch")
            payload = _parse_payload_bytes(recovery_payload_bytes)
            payload_hash = _sha256(_canonical_json_bytes(payload))
            _record_payload_for_table(table, payload)
            pk_fields = _pk_fields_from_table(table)
            pk_values = _identity_values_from_row(table, row)
            connection.execute(
                f"""
                UPDATE {table}
                SET
                    payload = ?,
                    payload_hash = ?,
                    status = 'committed',
                    error = '',
                    recovery_payload = NULL,
                    recovery_sha256 = ''
                WHERE {_where_clause(pk_fields)}
                """,
                (_canonical_json(payload), payload_hash, *pk_values),
            )
            return True
        except (TypeError, ValueError, KeyError):
            self._quarantine_row(connection, table, row, reason="staged_recovery_failed")
            return False

    def _is_committed_row_healthy(self, table: str, row: sqlite3.Row) -> bool:
        try:
            _assert_committed_record_integrity(table, row)
            return True
        except (TypeError, ValueError, KeyError):
            return False

    def _quarantine_row(self, connection: sqlite3.Connection, table: str, row: sqlite3.Row, reason: str) -> bool:
        pk_fields = _pk_fields_from_table(table)
        pk_values = tuple(_text(row[field]) for field in pk_fields)
        connection.execute(
            f"""
            UPDATE {table}
            SET status = 'quarantined', error = ?, recovery_payload = NULL, recovery_sha256 = ''
            WHERE {_where_clause(pk_fields)}
            """,
            (reason, *pk_values),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO data_plane_quarantine_audit(
                audit_key, record_kind, record_identity, reason, observed_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                f"{table}:{'|'.join(f'{field}={value}' for field, value in zip(pk_fields, pk_values, strict=False))}",
                _table_to_record_kind(table),
                "|".join(f"{field}={value}" for field, value in zip(pk_fields, pk_values, strict=False)),
                reason,
                _text(row["observed_at"]),
            ),
        )
        return True


def _profile(family: str) -> _Profile:
    if family not in _PROFILES:
        raise ValueError(f"unknown data-plane family: {family}")
    return _PROFILES[family]


def _pk_fields(profile: _Profile, mode: Mode) -> tuple[str, ...]:
    if mode == "formal":
        return ("freeze_id", *profile.identity_fields)
    return profile.identity_fields


def _where_clause(fields: tuple[str, ...]) -> str:
    return " AND ".join(f"{field} = ?" for field in fields)


def _identity_values(
    profile: _Profile,
    *,
    mode: Mode,
    freeze_id: str | None = None,
    record: Record | None = None,
    fields: dict[str, str] | None = None,
) -> tuple[str, ...]:
    values: list[str] = []
    if mode == "formal":
        if not freeze_id:
            raise ValueError("formal operations require freeze_id")
        values.append(freeze_id)

    if record is not None:
        values.extend(_record_identity_from_record(profile, record))
    elif fields is not None:
        for key in profile.identity_fields:
            if key not in fields:
                raise ValueError(f"missing identity field: {key}")
            values.append(str(fields[key]))
    else:
        raise ValueError("record or fields required")
    return tuple(values)


def _record_identity_from_record(profile: _Profile, record: Record) -> tuple[str, ...]:
    if profile.family == "security_master":
        if not isinstance(record, SecurityMasterRecord):
            raise TypeError("security master family requires SecurityMasterRecord")
        return (record.code,)
    if profile.family == "historical_feature":
        if not isinstance(record, HistoricalFeatureRecord):
            raise TypeError("historical feature family requires HistoricalFeatureRecord")
        return (record.code, record.trade_date)
    if profile.family == "risk_evidence":
        if not isinstance(record, RiskEvidenceRecord):
            raise TypeError("risk evidence family requires RiskEvidenceRecord")
        return (record.code, record.evidence_id)
    if profile.family == "source_cursor":
        if not isinstance(record, SourceCursorRecord):
            raise TypeError("source cursor family requires SourceCursorRecord")
        return (record.cursor_name,)
    raise ValueError(f"unknown family: {profile.family}")


def _record_to_row(profile: _Profile, record: Record, *, freeze_id: str | None = None) -> dict[str, object]:
    base: dict[str, object] = {}
    if profile.family == "security_master":
        if not isinstance(record, SecurityMasterRecord):
            raise TypeError("security master family requires SecurityMasterRecord")
        base["code"] = record.code
    elif profile.family == "historical_feature":
        if not isinstance(record, HistoricalFeatureRecord):
            raise TypeError("historical feature family requires HistoricalFeatureRecord")
        base["code"] = record.code
        base["trade_date"] = record.trade_date
    elif profile.family == "risk_evidence":
        if not isinstance(record, RiskEvidenceRecord):
            raise TypeError("risk evidence family requires RiskEvidenceRecord")
        base["code"] = record.code
        base["evidence_id"] = record.evidence_id
    elif profile.family == "source_cursor":
        if not isinstance(record, SourceCursorRecord):
            raise TypeError("source cursor family requires SourceCursorRecord")
        base["cursor_name"] = record.cursor_name
        base["cursor_value"] = record.cursor_value
    else:
        raise ValueError(f"unknown family: {profile.family}")
    if freeze_id is not None:
        base["freeze_id"] = freeze_id
    return base


def _record_payload_for_table(table: str, payload: JsonObject) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError("payload root must be an object")
    if table.startswith("security_master_"):
        pass
    elif table.startswith("historical_feature_"):
        pass
    elif table.startswith("risk_evidence_"):
        pass
    elif table.startswith("source_cursor_"):
        pass
    else:
        raise ValueError(f"unsupported table: {table}")
    if table.startswith("source_cursor_") and not payload:
        # source cursor payload may be empty but must remain a mapping
        return


def _row_payload(table: str, row: sqlite3.Row) -> JsonObject:
    payload = _parse_payload_text(_text(row["payload"]))
    _record_payload_for_table(table, payload)
    return payload


def _assert_committed_record_integrity(table: str, row: sqlite3.Row) -> None:
    payload = _parse_payload_text(_text(row["payload"]))
    _record_payload_for_table(table, payload)
    payload_text = _canonical_json(payload)
    if _sha256(payload_text.encode("utf-8")) != _text(row["payload_hash"]):
        raise ValueError("payload hash mismatch")
    _parse_datetime(_text(row["observed_at"]))
    _parse_datetime(_text(row["source_time"]))


def _row_to_record(table: str, row: sqlite3.Row) -> Record:
    payload = _row_payload(table, row)
    if table in {"security_master_recent", "security_master_formal"}:
        return SecurityMasterRecord(
            code=_text(row["code"]),
            observed_at=_parse_datetime(_text(row["observed_at"])),
            source_time=_parse_datetime(_text(row["source_time"])),
            source=_text(row["source"]),
            data_version=_text(row["data_version"]),
            payload=payload,
            payload_hash=_text(row["payload_hash"]),
            schema_version=_text(row["schema_version"]),
        )
    if table in {"historical_feature_recent", "historical_feature_formal"}:
        return HistoricalFeatureRecord(
            code=_text(row["code"]),
            trade_date=_text(row["trade_date"]),
            observed_at=_parse_datetime(_text(row["observed_at"])),
            source_time=_parse_datetime(_text(row["source_time"])),
            source=_text(row["source"]),
            data_version=_text(row["data_version"]),
            payload=payload,
            payload_hash=_text(row["payload_hash"]),
            schema_version=_text(row["schema_version"]),
        )
    if table in {"risk_evidence_recent", "risk_evidence_formal"}:
        return RiskEvidenceRecord(
            code=_text(row["code"]),
            evidence_id=_text(row["evidence_id"]),
            observed_at=_parse_datetime(_text(row["observed_at"])),
            source_time=_parse_datetime(_text(row["source_time"])),
            source=_text(row["source"]),
            data_version=_text(row["data_version"]),
            payload=payload,
            payload_hash=_text(row["payload_hash"]),
            schema_version=_text(row["schema_version"]),
        )
    if table in {"source_cursor_recent", "source_cursor_formal"}:
        return SourceCursorRecord(
            cursor_name=_text(row["cursor_name"]),
            cursor_value=_text(row["cursor_value"]),
            observed_at=_parse_datetime(_text(row["observed_at"])),
            source_time=_parse_datetime(_text(row["source_time"])),
            source=_text(row["source"]),
            data_version=_text(row["data_version"]),
            payload=payload,
            payload_hash=_text(row["payload_hash"]),
            schema_version=_text(row["schema_version"]),
        )
    raise ValueError(f"unsupported table: {table}")


def _table_to_record_kind(table: str) -> str:
    if table.startswith("security_master_"):
        return "security_master"
    if table.startswith("historical_feature_"):
        return "historical_feature"
    if table.startswith("risk_evidence_"):
        return "risk_evidence"
    if table.startswith("source_cursor_"):
        return "source_cursor"
    raise ValueError(f"unsupported table: {table}")


def _pk_fields_from_table(table: str) -> tuple[str, ...]:
    if table in {"security_master_formal", "historical_feature_formal", "risk_evidence_formal", "source_cursor_formal"}:
        return ("freeze_id", *_identity_fields_for_table(table))
    if table in {"security_master_recent", "historical_feature_recent", "risk_evidence_recent", "source_cursor_recent"}:
        return _identity_fields_for_table(table)
    raise ValueError(f"unsupported table: {table}")


def _identity_fields_for_table(table: str) -> tuple[str, ...]:
    if table.startswith("security_master_"):
        return ("code",)
    if table.startswith("historical_feature_"):
        return ("code", "trade_date")
    if table.startswith("risk_evidence_"):
        return ("code", "evidence_id")
    if table.startswith("source_cursor_"):
        return ("cursor_name",)
    raise ValueError(f"unsupported table: {table}")


def _identity_values_from_row(table: str, row: sqlite3.Row) -> tuple[str, ...]:
    return tuple(_text(row[field]) for field in _pk_fields_from_table(table))


def _to_payload_dict(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    return dict(payload)


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return _canonical_json(payload).encode("utf-8")


def _parse_payload_text(raw: str) -> JsonObject:
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise TypeError("payload root must be an object")
    return cast(JsonObject, decoded)


def _parse_payload_bytes(raw: bytes) -> JsonObject:
    return _parse_payload_text(raw.decode("utf-8"))


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if value is None:
        raise TypeError("required text field is missing")
    return str(value)


def _parse_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(_text(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _iso_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.isoformat()


def _map_sqlite_error(exc: sqlite3.OperationalError, *, operation: str) -> Exception:
    if "locked" in str(exc).lower():
        return DataPlaneUnavailableError(f"data plane {operation} blocked by database lock")
    return DataPlaneUnavailableError(f"data plane {operation} failed")


__all__ = ["DataPlaneRepository"]
