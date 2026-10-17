"""SQLite-backed persistence for the unified data-plane families."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, cast

from trader.recommendation.application.ports.data_plane_records import (
    DataPlaneBusyError,
    DataPlaneConflictError,
    DataPlaneRecoverySummary,
    DataPlaneUnavailableError,
    RiskEvidenceRecord,
    SecurityMasterRecord,
    SourceCursorRecord,
    TradingCalendarRecord,
)
from trader.recommendation.infra.persistence import data_plane_sqlite
from trader.recommendation.infra.persistence.data_plane_codec import (
    AssertCommittedRecordIntegrity as _assert_committed_record_integrity,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    CanonicalJson as _canonical_json,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    CanonicalJsonBytes as _canonical_json_bytes,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    IdentityFieldsForTable as _identity_fields_for_table,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    IdentityValuesFromRow as _identity_values_from_row,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    IsoDatetime as _iso_datetime,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    ParseDatetime as _parse_datetime,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    ParsePayloadBytes as _parse_payload_bytes,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    PkFieldsFromTable as _pk_fields_from_table,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    RecordPayloadForTable as _record_payload_for_table,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    RecordToRow as _record_to_row,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    RowToRecord as _row_to_record,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    Sha256 as _sha256,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    TableToRecordKind as _table_to_record_kind,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    Text as _text,
)
from trader.recommendation.infra.persistence.data_plane_codec import (
    ToPayloadDict as _to_payload_dict,
)
from trader.recommendation.infra.persistence.data_plane_types import (
    _DEFAULT_SCHEMA_VERSION,
    _MAX_PAYLOAD_BYTES,
    _PROFILES,
    Mode,
    Record,
    _Profile,
)


@dataclass(frozen=True)
class _PreparedWrite:
    record: Record
    row: dict[str, object]
    pk_fields: tuple[str, ...]
    pk_values: tuple[str, ...]
    payload_hash: str


class SQLiteDataPlane:
    """Persist and recover unified data-plane records."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        database_path: Path | None = None,
        buffered_writes: bool = False,
        read_only: bool = False,
    ) -> None:
        self._runtime_root = database_path.parent if database_path is not None else runtime_root / "market-data"
        self._database = database_path if database_path is not None else self._runtime_root / "market-data.sqlite3"
        self._lock = threading.RLock()
        self._initialized = False
        self._buffered_writes = buffered_writes
        self._read_only = read_only
        self._writer_connection: sqlite3.Connection | None = None

    def close(self) -> None:
        with self._lock:
            connection = self._writer_connection
            self._writer_connection = None
            if connection is not None:
                connection.close()

    @contextmanager
    def _write_scope(self) -> Iterator[sqlite3.Connection]:
        self._require_writable()
        if not self._buffered_writes:
            with data_plane_sqlite.ConnectionScope(self._database) as connection:
                yield connection
            return
        writer_connection = self._writer_connection
        if writer_connection is None:
            writer_connection = data_plane_sqlite.Connect(self._database, cross_thread=True)
            try:
                writer_connection.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError:
                writer_connection.close()
                raise
            self._writer_connection = writer_connection
        with writer_connection:
            yield writer_connection

    def initialize(self) -> None:
        with self._lock:
            if self._initialized and self._database.is_file():
                return
            try:
                if self._read_only:
                    with data_plane_sqlite.ReadConnectionScope(self._database) as connection:
                        row = connection.execute(
                            "SELECT value FROM schema_meta WHERE key = ?",
                            (data_plane_sqlite.SCHEMA_META_KEY,),
                        ).fetchone()
                    if row is None or int(row["value"]) != data_plane_sqlite.SCHEMA_VERSION:
                        raise DataPlaneUnavailableError("read-only data plane schema is unavailable")
                    self._initialized = True
                    return
                self._runtime_root.mkdir(parents=True, exist_ok=True)
                data_plane_sqlite.InitializeDatabase(self._database)
            except DataPlaneUnavailableError:
                raise
            except sqlite3.DatabaseError as exc:
                raise _map_sqlite_error(exc, operation="initialize") from exc
            except (TypeError, ValueError) as exc:
                raise DataPlaneUnavailableError("read-only data plane schema is unavailable") from exc
            self._initialized = True

    def probe_writable(self, *, timeout_seconds: float = 8.0) -> None:
        """Check the SQLite write lock without changing application data."""
        self._require_writable()
        if not self._initialized or not self._database.is_file():
            self.initialize()
        started = time.monotonic()
        wait_seconds = min(30.0, max(0.0, timeout_seconds))
        if not self._lock.acquire(timeout=wait_seconds):
            raise DataPlaneBusyError("data plane write_probe blocked by local writer")
        try:
            with data_plane_sqlite.ConnectionScope(self._database) as connection:
                remaining_ms = max(0, int((wait_seconds - (time.monotonic() - started)) * 1000))
                connection.execute(f"PRAGMA busy_timeout={remaining_ms}")
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
        except sqlite3.DatabaseError as exc:
            raise _map_sqlite_error(exc, operation="write_probe") from exc
        finally:
            self._lock.release()

    def save_security_master_recent(self, record: SecurityMasterRecord) -> None:
        self._save("security_master", mode="recent", record=record)

    def save_security_master_recent_records(self, records: Sequence[SecurityMasterRecord]) -> None:
        self._save_many("security_master", mode="recent", records=records)

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

    def save_risk_evidence_recent(self, record: RiskEvidenceRecord) -> None:
        self._save("risk_evidence", mode="recent", record=record)

    def save_risk_evidence_recent_records(self, records: Sequence[RiskEvidenceRecord]) -> None:
        self._save_many("risk_evidence", mode="recent", records=records)

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

    def scan_risk_evidence_recent_records(
        self,
        consume: Callable[[RiskEvidenceRecord], None],
        codes: Sequence[str] | None = None,
        *,
        research_only: bool = False,
    ) -> None:
        self.initialize()
        selected = tuple(sorted(set(codes))) if codes is not None else None
        if selected == ():
            return
        selected_set = set(selected) if selected is not None else None
        with self._lock:
            try:
                with self._read_scope() as connection:
                    evidence_filter = (
                        " AND (evidence_id = 'risk-observation:structured' "
                        "OR (evidence_id >= 'risk-component:' AND evidence_id < 'risk-component;'))"
                        if research_only
                        else ""
                    )
                    sort_order = "evidence_id, code" if research_only else "code, evidence_id"
                    if selected is not None and len(selected) <= 900:
                        placeholders = ", ".join("?" for _ in selected)
                        query = (
                            "SELECT * FROM risk_evidence_recent WHERE status = 'committed' "
                            f"AND code IN ({placeholders}){evidence_filter} ORDER BY {sort_order}"
                        )
                        rows = connection.execute(query, selected)
                    else:
                        rows = connection.execute(
                            "SELECT * FROM risk_evidence_recent WHERE status = 'committed'"
                            f"{evidence_filter} ORDER BY {sort_order}"
                        )
                    for row in rows:
                        if (
                            selected_set is not None
                            and len(selected_set) > 900
                            and _text(row["code"]) not in selected_set
                        ):
                            continue
                        try:
                            _assert_committed_record_integrity("risk_evidence_recent", row)
                            record = cast(RiskEvidenceRecord, _row_to_record("risk_evidence_recent", row))
                        except (TypeError, ValueError, KeyError):
                            self._quarantine_row(
                                connection, "risk_evidence_recent", row, reason="load_verification_failed"
                            )
                            continue
                        consume(record)
            except sqlite3.DatabaseError as exc:
                raise _map_sqlite_error(exc, operation="load") from exc

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

    def save_trading_calendar_recent(self, record: TradingCalendarRecord) -> None:
        self._save("trading_calendar", mode="recent", record=record)

    def save_trading_calendar_formal(self, freeze_id: str, record: TradingCalendarRecord) -> None:
        self._save("trading_calendar", mode="formal", freeze_id=freeze_id, record=record)

    def load_trading_calendar_recent(self, calendar_name: str) -> TradingCalendarRecord | None:
        return cast(
            TradingCalendarRecord | None,
            self._load("trading_calendar", mode="recent", calendar_name=calendar_name),
        )

    def load_trading_calendar_recent_records(
        self,
        calendar_names: Sequence[str] | None = None,
    ) -> tuple[TradingCalendarRecord, ...]:
        return cast(
            tuple[TradingCalendarRecord, ...],
            self._load_records("trading_calendar", mode="recent", calendar_names=calendar_names),
        )

    def load_trading_calendar_formal(
        self,
        freeze_id: str,
        calendar_name: str,
    ) -> TradingCalendarRecord | None:
        return cast(
            TradingCalendarRecord | None,
            self._load("trading_calendar", mode="formal", freeze_id=freeze_id, calendar_name=calendar_name),
        )

    def load_trading_calendar_formal_records(
        self,
        freeze_id: str,
        calendar_names: Sequence[str] | None = None,
    ) -> tuple[TradingCalendarRecord, ...]:
        return cast(
            tuple[TradingCalendarRecord, ...],
            self._load_records(
                "trading_calendar",
                mode="formal",
                freeze_id=freeze_id,
                calendar_names=calendar_names,
            ),
        )

    def recover(self) -> DataPlaneRecoverySummary:
        self._require_writable()
        self.initialize()
        recovered = 0
        quarantined = 0
        orphaned = 0

        with self._lock:
            try:
                with data_plane_sqlite.ConnectionScope(self._database) as connection:
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
            except sqlite3.DatabaseError as exc:
                raise _map_sqlite_error(exc, operation="recovery") from exc

        return DataPlaneRecoverySummary(recovered=recovered, quarantined=quarantined, orphaned=orphaned)

    def _save(self, family: str, *, mode: Mode, record: Record, freeze_id: str | None = None) -> None:
        self._save_many(family, mode=mode, records=(record,), freeze_id=freeze_id)

    def _save_many(
        self,
        family: str,
        *,
        mode: Mode,
        records: Sequence[Record],
        freeze_id: str | None = None,
    ) -> None:
        self._require_writable()
        pending = tuple(records)
        if not pending:
            return
        self.initialize()
        profile = _profile(family)
        table = profile.formal_table if mode == "formal" else profile.recent_table
        if mode == "formal" and not freeze_id:
            raise ValueError("formal save requires freeze_id")
        if mode != "formal" and freeze_id is not None:
            raise ValueError("recent save must not provide freeze_id")

        writes = tuple(_prepare_write(profile, mode=mode, record=record, freeze_id=freeze_id) for record in pending)

        with self._lock:
            try:
                with self._write_scope() as connection:
                    for write in writes:
                        _execute_write(connection, table=table, mode=mode, write=write)
            except sqlite3.DatabaseError as exc:
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

        with self._lock:
            try:
                with self._read_scope() as connection:
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
            except sqlite3.DatabaseError as exc:
                raise _map_sqlite_error(exc, operation="load") from exc

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
        calendar_names: Sequence[str] | None = None,
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
        calendar_filter = tuple(sorted(set(calendar_names))) if calendar_names else None

        query, params = _record_select_query(table, mode, freeze_id, code_filter, cursor_filter)
        query += f" ORDER BY {', '.join(_identity_fields_for_table(table))}"

        result: list[Record] = []

        with self._lock:
            try:
                with self._read_scope() as connection:
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
                        if calendar_filter is not None:
                            candidate = _text(row["calendar_name"])
                            if candidate not in calendar_filter:
                                continue
                        try:
                            _assert_committed_record_integrity(table, row)
                            result.append(_row_to_record(table, row))
                        except (TypeError, ValueError, KeyError):
                            self._quarantine_row(connection, table, row, reason="load_verification_failed")
            except sqlite3.DatabaseError as exc:
                raise _map_sqlite_error(exc, operation="load") from exc

        return tuple(result)

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
        if self._read_only:
            return False
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

    @contextmanager
    def _read_scope(self) -> Iterator[sqlite3.Connection]:
        scope = data_plane_sqlite.ReadConnectionScope if self._read_only else data_plane_sqlite.ConnectionScope
        with scope(self._database) as connection:
            yield connection

    def _require_writable(self) -> None:
        if self._read_only:
            raise DataPlaneUnavailableError("read-only data plane cannot be modified")


def _record_select_query(
    table: str,
    mode: Mode,
    freeze_id: str | None,
    code_filter: tuple[str, ...] | None,
    cursor_filter: tuple[str, ...] | None,
) -> tuple[str, tuple[object, ...]]:
    query = f"SELECT * FROM {table} WHERE status = ?"
    params: tuple[object, ...] = ("committed",)
    if mode == "formal":
        query += " AND freeze_id = ?"
        params += (freeze_id,)
    if code_filter is not None and len(code_filter) <= 900:
        query += f" AND code IN ({', '.join('?' for _ in code_filter)})"
        params += code_filter
    if cursor_filter is not None and len(cursor_filter) <= 900:
        query += f" AND cursor_name IN ({', '.join('?' for _ in cursor_filter)})"
        params += cursor_filter
    return query, params


def _profile(family: str) -> _Profile:
    if family not in _PROFILES:
        raise ValueError(f"unknown data-plane family: {family}")
    return _PROFILES[family]


def _record_metadata_matches(row: sqlite3.Row, record: Record, payload_hash: str) -> bool:
    return (
        _text(row["observed_at"]) == _iso_datetime(record.observed_at)
        and _text(row["source_time"]) == _iso_datetime(record.source_time)
        and _text(row["source"]) == record.source
        and _text(row["data_version"]) == record.data_version
        and _text(row["schema_version"]) == (record.schema_version or _DEFAULT_SCHEMA_VERSION)
        and _text(row["payload_hash"]) == payload_hash
    )


def _prepare_write(
    profile: _Profile,
    *,
    mode: Mode,
    record: Record,
    freeze_id: str | None,
) -> _PreparedWrite:
    payload_text = _canonical_json(_to_payload_dict(record.payload))
    payload_bytes = payload_text.encode("utf-8")
    if len(payload_bytes) > _MAX_PAYLOAD_BYTES:
        raise DataPlaneUnavailableError("data plane payload exceeds maximum bytes")
    payload_hash = _sha256(payload_bytes)
    if record.payload_hash and record.payload_hash != payload_hash:
        raise ValueError("payload_hash must match canonical payload")
    row = _record_to_row(profile, record, freeze_id=freeze_id)
    row.update(
        {
            "observed_at": _iso_datetime(record.observed_at),
            "source_time": _iso_datetime(record.source_time),
            "source": record.source,
            "data_version": record.data_version,
            "schema_version": record.schema_version or _DEFAULT_SCHEMA_VERSION,
            "payload_hash": payload_hash,
            "payload": payload_text,
            "status": "committed",
            "error": "",
            "recovery_payload": None,
            "recovery_sha256": "",
        }
    )
    return _PreparedWrite(
        record=record,
        row=row,
        pk_fields=_pk_fields(profile, mode),
        pk_values=_identity_values(profile, mode=mode, freeze_id=freeze_id, record=record),
        payload_hash=payload_hash,
    )


def _execute_write(
    connection: sqlite3.Connection,
    *,
    table: str,
    mode: Mode,
    write: _PreparedWrite,
) -> None:
    columns = tuple(write.row)
    placeholders = ", ".join("?" for _ in columns)
    inserted = connection.execute(
        f"""
        INSERT INTO {table} ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT({", ".join(write.pk_fields)}) DO NOTHING
        """,
        tuple(write.row[field] for field in columns),
    )
    if inserted.rowcount:
        return
    identity = (write.pk_fields, write.pk_values)
    allowed = (
        _formal_write_allowed(
            connection=connection,
            table=table,
            identity=identity,
            record=write.record,
            payload_hash=write.payload_hash,
        )
        if mode == "formal"
        else _recent_write_allowed(
            connection=connection,
            table=table,
            identity=identity,
            record=write.record,
            payload_hash=write.payload_hash,
        )
    )
    if not allowed:
        return
    update_fields = tuple(field for field in columns if field not in write.pk_fields)
    updates = ", ".join(f"{field}= ?" for field in update_fields)
    connection.execute(
        f"""
        UPDATE {table} SET {updates}
        WHERE {_where_clause(write.pk_fields)}
        """,
        (*tuple(write.row[field] for field in update_fields), *write.pk_values),
    )


def _formal_write_allowed(
    *,
    connection: sqlite3.Connection,
    table: str,
    identity: tuple[tuple[str, ...], tuple[str, ...]],
    record: Record,
    payload_hash: str,
) -> bool:
    pk_fields, pk_values = identity
    existing = connection.execute(
        f"SELECT * FROM {table} WHERE {_where_clause(pk_fields)}",
        pk_values,
    ).fetchone()
    if existing is None or _text(existing["status"]) != "committed":
        return True
    if _record_metadata_matches(existing, record, payload_hash):
        return False
    raise DataPlaneConflictError("formal save conflicts with existing committed record")


def _recent_write_allowed(
    *,
    connection: sqlite3.Connection,
    table: str,
    identity: tuple[tuple[str, ...], tuple[str, ...]],
    record: Record,
    payload_hash: str,
) -> bool:
    pk_fields, pk_values = identity
    existing = connection.execute(
        f"SELECT * FROM {table} WHERE {_where_clause(pk_fields)}",
        pk_values,
    ).fetchone()
    if existing is None or _text(existing["status"]) != "committed":
        return True
    existing_observed_at = _parse_datetime(existing["observed_at"])
    if record.observed_at < existing_observed_at:
        return False
    if record.observed_at > existing_observed_at:
        return True
    if _record_metadata_matches(existing, record, payload_hash):
        return False
    raise DataPlaneConflictError("recent save conflicts at the same observation time")


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
    row = _record_to_row(profile, record)
    return tuple(_text(row[field]) for field in profile.identity_fields)


def _map_sqlite_error(exc: sqlite3.DatabaseError, *, operation: str) -> DataPlaneUnavailableError:
    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
        return DataPlaneBusyError(f"data plane {operation} blocked by database lock")
    return DataPlaneUnavailableError(f"data plane {operation} failed")


__all__ = ["SQLiteDataPlane"]
