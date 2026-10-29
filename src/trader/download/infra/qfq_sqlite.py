"""Shared structured prices with atomic profile identities and completion facts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import date
from pathlib import Path
from typing import TypeAlias
from uuid import uuid4

from trader.download.application.update_qfq import QfqPairPublicationError, QfqWindowPort
from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow
from trader.download.domain.price_repair import PriceIssue
from trader.download.infra.price_repair_attempts import PRICE_REPAIR_ATTEMPT_SQL, PRICE_SOURCE_CHECK_SQL, PriceRepairAttempts, RecordTencentCheck, TencentRecheckDue
from trader.download.infra.published_history_codec import EncodePublishedHistoryCell, DecodePublishedHistoryCellPayload
from trader.download.domain.qfq_window import QFQ_WINDOWS, QfqWindowSnapshot, QfqWindowState
from trader.download.infra.history_sqlite_read_connection import WatchHistoryRead
from trader.download.infra.price_columns import (
    PRICE_COLUMNS,
    PRICE_WINDOW_SQL,
    DecodePriceValues,
    PriceCellFromColumns,
    PriceColumnValues,
    PriceValues,
)
from trader.infra.sqlite_codec import DecodeDate, DecodeHash, EncodeDate, EncodeHash
from trader.infra.sqlite_connection import (
    CheckSqliteIntegrity,
    EnsureSqliteSchema,
    HasSqliteSchema,
    OpenSqliteConnection,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteTransaction,
)

_Rows: TypeAlias = tuple[tuple[int, PriceValues], ...]
_SELECT = ",".join(PRICE_COLUMNS)
_BAR_ROW_PARAMETERS = len(PRICE_COLUMNS) + 2
# 38 structured rows use 988 parameters, below SQLite's older 999-parameter limit.
_INSERT_ROWS_PER_STATEMENT = 38
_INSERT_ROW_PLACEHOLDERS = "(" + ",".join("?" for _ in range(_BAR_ROW_PARAMETERS)) + ")"
_SCHEMA = SqliteSchema(
    version=3,
    statements=(
        f"CREATE TABLE daily_price(code TEXT NOT NULL, trade_date INTEGER NOT NULL, {PRICE_WINDOW_SQL}, PRIMARY KEY(code,trade_date)) WITHOUT ROWID",
        "CREATE TABLE window_binding(profile TEXT NOT NULL, code TEXT NOT NULL, content_hash BLOB NOT NULL CHECK(length(content_hash)=32), "
        "source TEXT NOT NULL, start_date INTEGER NOT NULL, end_date INTEGER NOT NULL, "
        "PRIMARY KEY(profile,code)) WITHOUT ROWID",
        "CREATE TABLE window_calendar(profile TEXT NOT NULL, trade_date INTEGER NOT NULL, PRIMARY KEY(profile,trade_date)) WITHOUT ROWID",
        "CREATE TABLE window_completion(code TEXT PRIMARY KEY, cutoff_date INTEGER NOT NULL, source TEXT NOT NULL) WITHOUT ROWID",
        "CREATE TABLE window_publication(profile TEXT PRIMARY KEY, publication_id TEXT NOT NULL) WITHOUT ROWID",
    ),
)
_REPAIR_SQL = (
    PRICE_SOURCE_CHECK_SQL,
    PRICE_REPAIR_ATTEMPT_SQL.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1),
    "CREATE TABLE IF NOT EXISTS price_issue(code TEXT NOT NULL, trade_date INTEGER NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(code,trade_date)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS price_candidate(code TEXT NOT NULL, trade_date INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(code,trade_date)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS price_nontrading(code TEXT NOT NULL, trade_date INTEGER NOT NULL, PRIMARY KEY(code,trade_date)) WITHOUT ROWID",
)


def _validate_code(code: str) -> None:
    if (
        len(code) != 6
        or not code.isascii()
        or not code.isdigit()
        or not code.startswith(
            ("600", "601", "603", "605", "000", "001", "002", "003", "300", "301", "302", "688", "689")
        )
    ):
        raise ValueError("qfq_code_invalid")


def _digest(rows: _Rows) -> str:
    try:
        canonical = tuple((DecodeDate(day).isoformat(), DecodePriceValues(values)) for day, values in rows)
        payload = json.dumps(canonical, separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("qfq_code_identity_mismatch") from exc
    return hashlib.sha256(payload).hexdigest()


class SQLiteQfqWindowCache:
    """Profile view; construction and absent reads have no filesystem side effects."""

    def __init__(self, path: Path, profile: str) -> None:
        if profile not in dict(QFQ_WINDOWS):
            raise ValueError("qfq_profile_invalid")
        self.path = path
        self.profile = profile
        self.sessions = dict(QFQ_WINDOWS)[profile]

    def _connection(self, *, write: bool = False) -> SqliteConnection:
        if self.path.is_symlink() or self.path.is_dir():
            raise RuntimeError("qfq_path_unsafe")
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = OpenSqliteConnection(
            self.path,
            SqliteConnectionOptions(
                read_only=not write,
                journal_mode="WAL" if write else None,
            ),
        )
        return connection

    @contextmanager
    def _read_connection(self) -> Iterator[SqliteConnection | None]:
        if not self.path.exists():
            yield None
            return
        with closing(self._connection()) as connection:
            # A rolled-back first publication has no schema or visible windows.
            # Any populated or versioned incompatible database still fails closed.
            yield connection if HasSqliteSchema(connection, _SCHEMA) else None

    @contextmanager
    def _write_connection(self) -> Iterator[SqliteConnection]:
        with closing(self._connection(write=True)) as connection, SqliteTransaction(connection, immediate=True):
            EnsureSqliteSchema(connection, _SCHEMA, allow_create=True)
            for statement in _REPAIR_SQL:
                connection.run(statement)
            yield connection

    def price_issues(self) -> tuple[PriceIssue, ...]:
        with self._read_connection() as connection:
            if connection is None or connection.run("SELECT 1 FROM sqlite_master WHERE name='price_issue'").fetchone() is None:
                return ()
            grouped: dict[str, list[date]] = {}
            reasons: dict[str, str] = {}
            for code, day, reason in connection.run("SELECT code,trade_date,reason FROM price_issue ORDER BY code,trade_date"):
                grouped.setdefault(code, []).append(DecodeDate(day))
                reasons[code] = reason
        return tuple(PriceIssue(code, tuple(days), reasons[code]) for code, days in grouped.items())

    def repair_cutoff(self) -> date | None:
        with self._read_connection() as connection:
            if connection is None:
                return None
            row = connection.run("SELECT MAX(trade_date) FROM window_calendar WHERE profile=?", (self.profile,)).require_one()
            return DecodeDate(row[0]) if row[0] is not None else None

    def record_price_issue(self, issue: PriceIssue, observed: PublishedHistoryWindow | None = None) -> None:
        _validate_code(issue.code)
        with self._write_connection() as connection:
            connection.run_many("INSERT OR REPLACE INTO price_issue VALUES(?,?,?)", ((issue.code, EncodeDate(day), issue.reason) for day in issue.dates))
            connection.run("DELETE FROM window_completion WHERE code=?", (issue.code,))
            if observed is not None:
                if observed.code != issue.code:
                    raise ValueError("price_issue_identity_conflict")
                connection.run_many("INSERT OR REPLACE INTO price_candidate VALUES(?,?,?)", ((issue.code, EncodeDate(cell.trade_date), EncodePublishedHistoryCell(cell)) for cell in observed.cells))

    def covered_dates(self, code: str) -> tuple[date, ...]:
        days = {cell.trade_date for cell in self.read_code(code).cells}
        with self._read_connection() as connection:
            if connection is not None and connection.run("SELECT 1 FROM sqlite_master WHERE name='price_nontrading'").fetchone():
                days.update(DecodeDate(row[0]) for row in connection.run("SELECT trade_date FROM price_nontrading WHERE code=?", (code,)))
        return tuple(sorted(days))

    def repair_reference(self, code: str) -> PublishedHistoryWindow:
        cells = {cell.trade_date: cell for cell in self.read_code(code).cells}
        with self._read_connection() as connection:
            if connection is not None and connection.run("SELECT 1 FROM sqlite_master WHERE name='price_candidate'").fetchone():
                for day, payload in connection.run("SELECT trade_date,payload FROM price_candidate WHERE code=?", (code,)):
                    cell = DecodePublishedHistoryCellPayload(code, DecodeDate(day).isoformat(), payload)
                    if cell.unadjusted is not None:
                        cells[cell.trade_date] = cell
        return PublishedHistoryWindow(code, tuple(cells[day] for day in sorted(cells)))

    def reserve_repair(self, issue: PriceIssue, batch: str, *, explicit: bool) -> bool:
        with self._write_connection() as connection:
            return bool(PriceRepairAttempts(connection).reserve(issue.code, issue.dates, batch, explicit=explicit))

    def tencent_recheck_due(self, code: str, calendar: tuple[date, ...]) -> bool:
        with self._write_connection() as connection:
            due = TencentRecheckDue(connection, code, calendar)
            if due:
                RecordTencentCheck(connection, code, calendar[-1])
            return due

    def fail_repair(self, code: str, batch: str, reason: str) -> None:
        with self._write_connection() as connection:
            PriceRepairAttempts(connection).fail(code, batch, reason.lower())
            connection.run("UPDATE price_issue SET reason=? WHERE code=?", (reason, code))

    def verify_integrity(self) -> None:
        with self._read_connection() as connection:
            if connection is not None:
                CheckSqliteIntegrity(connection)

    def codes(self) -> frozenset[str]:
        with self._read_connection() as connection:
            if connection is None:
                return frozenset()
            return frozenset(
                row[0] for row in connection.run("SELECT code FROM window_binding WHERE profile=?", (self.profile,))
            )

    def completed(self, day: date, code: str, source: str) -> bool:
        with self._read_connection() as connection:
            if connection is None:
                return False
            return (
                connection.run(
                    "SELECT 1 FROM window_completion WHERE code=? AND cutoff_date=? AND source=?",
                    (code, EncodeDate(day), source),
                ).fetchone()
                is not None
            )

    def _rows(self, connection: SqliteConnection, code: str) -> _Rows:
        identity = connection.run(
            "SELECT content_hash,start_date,end_date FROM window_binding WHERE profile=? AND code=?",
            (self.profile, code),
        ).fetchone()
        if identity is None:
            return ()
        rows = tuple(
            (row[0], tuple(row[1:]))
            for row in connection.run(
                f"SELECT trade_date,{_SELECT} FROM daily_price WHERE code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                (code, identity[1], identity[2]),
            )
        )
        if len(rows) > self.sessions or _digest(rows) != DecodeHash(identity[0]):
            raise RuntimeError("qfq_code_identity_mismatch")
        return rows

    def _batch_rows(self, connection: SqliteConnection, codes: tuple[str, ...]) -> tuple[tuple[str, _Rows], ...]:
        placeholders = ",".join("?" for _ in codes)
        identities = {
            str(code): DecodeHash(digest)
            for code, digest in connection.run(
                f"SELECT code,content_hash FROM window_binding WHERE profile=? AND code IN ({placeholders})",
                (self.profile, *codes),
            )
        }
        grouped: dict[str, list[tuple[int, PriceValues]]] = {code: [] for code in identities}
        for code, day, *values in connection.run(
            f"SELECT daily_price.code,daily_price.trade_date,{_SELECT} FROM window_binding AS identity "
            "JOIN daily_price ON daily_price.code=identity.code "
            "AND daily_price.trade_date BETWEEN identity.start_date AND identity.end_date "
            f"WHERE identity.profile=? AND identity.code IN ({placeholders}) ORDER BY daily_price.code,daily_price.trade_date",
            (self.profile, *codes),
        ):
            grouped[str(code)].append((day, tuple(values)))
        result = []
        for code in codes:
            rows = tuple(grouped.get(code, ()))
            digest = identities.get(code)
            if digest is not None and (len(rows) > self.sessions or _digest(rows) != digest):
                raise RuntimeError("qfq_code_identity_mismatch")
            result.append((code, rows))
        return tuple(result)

    def read_code(self, code: str) -> PublishedHistoryWindow:
        return self.read_codes((code,))[0]

    def read_codes(self, codes: Sequence[str]) -> tuple[PublishedHistoryWindow, ...]:
        requested = tuple(sorted(set(codes)))
        for code in requested:
            _validate_code(code)
        result: list[PublishedHistoryWindow] = []
        try:
            with self._read_connection() as connection:
                if connection is None:
                    return tuple(PublishedHistoryWindow(code, ()) for code in requested)
                for start in range(0, len(requested), 32):
                    batch = requested[start : start + 32]
                    with SqliteTransaction(connection):
                        raw_rows = self._batch_rows(connection, batch)
                    result.extend(
                        PublishedHistoryWindow(
                            code, tuple(PriceCellFromColumns(code, day, values) for day, values in rows)
                        )
                        for code, rows in raw_rows
                    )
        except sqlite3.Error as exc:
            raise RuntimeError("qfq_read_failed") from exc
        return tuple(result)

    def inspect_windows(self, allowed_codes: frozenset[str]) -> QfqWindowSnapshot:
        states: list[QfqWindowState] = []
        with self._read_connection() as connection:
            if connection is None:
                return QfqWindowSnapshot(())
            identities = connection.run(
                "SELECT code,source FROM window_binding WHERE profile=? ORDER BY code", (self.profile,)
            ).fetchall()
            for code, source in identities:
                if code not in allowed_codes:
                    continue
                try:
                    with SqliteTransaction(connection):
                        rows = self._rows(connection, code)
                    states.append(QfqWindowState(code, source, tuple(DecodeDate(day) for day, _ in rows), True))
                except (RuntimeError, ValueError):
                    states.append(QfqWindowState(code, source, (), False))
        return QfqWindowSnapshot(tuple(states))

    def source_identity(self, code: str) -> str | None:
        with self._read_connection() as connection:
            if connection is None:
                return None
            row = connection.run(
                "SELECT source FROM window_binding WHERE profile=? AND code=?", (self.profile, code)
            ).fetchone()
        return str(row[0]) if row else None

    def retain_codes(self, allowed_codes: frozenset[str]) -> tuple[tuple[str, ...], int]:
        for code in allowed_codes:
            _validate_code(code)
        excluded = self.codes() - allowed_codes
        if not excluded:
            return (), 0
        with self._write_connection() as connection:
            connection.run_many(
                "DELETE FROM window_binding WHERE profile=? AND code=?", ((self.profile, code) for code in excluded)
            )
            connection.run_many("DELETE FROM window_completion WHERE code=?", ((code,) for code in excluded))
            deleted = connection.run(
                "DELETE FROM daily_price WHERE code NOT IN (SELECT code FROM window_binding)"
            ).rowcount
            self._publish_identity(connection)
        return (self.path.name,), deleted

    def _matches_windows(
        self,
        peer: SQLiteQfqWindowCache,
        targets: tuple[tuple[str, _Rows], ...],
        source: str,
        calendar: tuple[date, ...],
        completed_day: date | None,
    ) -> bool:
        with self._read_connection() as connection:
            if connection is None:
                return False
            with SqliteTransaction(connection):
                for code, target in targets:
                    actual = tuple(
                        (row[0], tuple(row[1:]))
                        for row in connection.run(
                            f"SELECT trade_date,{_SELECT} FROM daily_price WHERE code=? ORDER BY trade_date", (code,)
                        )
                    )
                    if actual != target:
                        return False
                for cache in (self, peer):
                    days = calendar[-cache.sessions :]
                    if tuple(
                        row[0]
                        for row in connection.run(
                            "SELECT trade_date FROM window_calendar WHERE profile=? ORDER BY trade_date",
                            (cache.profile,),
                        )
                    ) != tuple(EncodeDate(day) for day in days):
                        return False
                    for code, target in targets:
                        rows = tuple((day, values) for day, values in target if day >= EncodeDate(days[0]))
                        identity = connection.run(
                            "SELECT content_hash,source,start_date,end_date FROM window_binding WHERE profile=? AND code=?",
                            (cache.profile, code),
                        ).fetchone()
                        if identity is None or tuple(identity) != (
                            EncodeHash(_digest(rows)),
                            source,
                            EncodeDate(days[0]),
                            EncodeDate(days[-1]),
                        ):
                            return False
                if completed_day is not None:
                    for code, _rows in targets:
                        if (
                            connection.run(
                                "SELECT 1 FROM window_completion WHERE code=? AND cutoff_date=? AND source=?",
                                (code, EncodeDate(completed_day), source),
                            ).fetchone()
                            is None
                        ):
                            return False
        return True

    def replace_paired_windows(
        self,
        peer: QfqWindowPort,
        windows: tuple[PublishedHistoryWindow, ...],
        source_identity: str,
        *,
        calendar: tuple[date, ...],
        completed_day: date | None,
    ) -> tuple[tuple[str, ...], int]:
        if (
            not isinstance(peer, SQLiteQfqWindowCache)
            or self.path != peer.path
            or {self.profile, peer.profile} != {"v2", "v3"}
        ):
            raise ValueError("qfq_pair_layout_invalid")
        if not calendar or calendar != tuple(sorted(set(calendar))) or len(calendar) > 251:
            raise ValueError("qfq_calendar_invalid")
        if completed_day is not None and completed_day != calendar[-1]:
            raise ValueError("qfq_completion_day_invalid")
        allowed_dates = frozenset(calendar)
        for window in windows:
            _validate_code(window.code)
            if any(cell.trade_date not in allowed_dates for cell in window.cells):
                raise ValueError("qfq_window_outside_calendar")
        changed, count = False, 0
        try:
            targets = tuple(
                (
                    window.code,
                    tuple(
                        (EncodeDate(cell.trade_date), PriceColumnValues(cell))
                        for cell in window.cells
                        if cell.status == "complete"
                    ),
                )
                for window in windows
            )
            if not self.price_issues() and self._matches_windows(peer, targets, source_identity, calendar, completed_day):
                return (), 0
            with self._write_connection() as connection:
                for code, target in targets:
                    RecordTencentCheck(connection, code, calendar[-1], initial=source_identity.startswith("baostock:"))
                    window = next(item for item in windows if item.code == code)
                    obtained = tuple(cell.trade_date for cell in window.cells if cell.status in ("complete", "supplier_marked_suspended", "tencent_inferred_suspended"))
                    connection.run("DELETE FROM price_nontrading WHERE code=?", (code,))
                    connection.run_many("INSERT INTO price_nontrading VALUES(?,?)", ((code, EncodeDate(cell.trade_date)) for cell in window.cells if cell.status in ("supplier_marked_suspended", "tencent_inferred_suspended")))
                    connection.run_many("DELETE FROM price_candidate WHERE code=? AND trade_date=?", ((code, EncodeDate(day)) for day in obtained))
                    connection.run_many("DELETE FROM price_issue WHERE code=? AND trade_date=?", ((code, EncodeDate(day)) for day in obtained))
                    PriceRepairAttempts(connection).resolve(code, obtained, "baostock" if source_identity.startswith("baostock:") else "tencent")
                    incomplete = tuple(cell.trade_date for cell in window.cells if cell.trade_date not in obtained)
                    connection.run_many("INSERT OR REPLACE INTO price_issue VALUES(?,?,'price_window_incomplete')", ((code, EncodeDate(day)) for day in incomplete))
                    unresolved = connection.run("SELECT 1 FROM price_issue WHERE code=? AND trade_date BETWEEN ? AND ? LIMIT 1", (code, EncodeDate(calendar[0]), EncodeDate(calendar[-1]))).fetchone() is not None
                    old = tuple(
                        (row[0], tuple(row[1:]))
                        for row in connection.run(
                            f"SELECT trade_date,{_SELECT} FROM daily_price WHERE code=? ORDER BY trade_date", (code,)
                        )
                    )
                    before, after = dict(old), dict(target)
                    count += sum(before.get(day) != after.get(day) for day in before.keys() | after.keys())
                    if old != target:
                        changed = True
                        connection.run_many(
                            "DELETE FROM daily_price WHERE code=? AND trade_date=?",
                            ((code, day) for day in before.keys() - after.keys()),
                        )
                        inserts = tuple((day, values) for day, values in target if before.get(day) != values)
                        insert_command = (
                            "INSERT OR REPLACE INTO daily_price VALUES "
                            if before
                            else "INSERT INTO daily_price VALUES "
                        )
                        for start in range(0, len(inserts), _INSERT_ROWS_PER_STATEMENT):
                            batch = inserts[start : start + _INSERT_ROWS_PER_STATEMENT]
                            connection.run(
                                insert_command + ",".join(_INSERT_ROW_PLACEHOLDERS for _ in batch),
                                tuple(value for day, values in batch for value in (code, day, *values)),
                            )
                    for cache in (self, peer):
                        days = calendar[-cache.sessions :]
                        rows = tuple((day, values) for day, values in target if day >= EncodeDate(days[0]))
                        identity = (
                            EncodeHash(_digest(rows)),
                            source_identity,
                            EncodeDate(days[0]),
                            EncodeDate(days[-1]),
                        )
                        previous = connection.run(
                            "SELECT content_hash,source,start_date,end_date FROM window_binding WHERE profile=? AND code=?",
                            (cache.profile, code),
                        ).fetchone()
                        if previous is None or tuple(previous) != identity:
                            changed = True
                            connection.run(
                                "INSERT OR REPLACE INTO window_binding VALUES (?,?,?,?,?,?)",
                                (cache.profile, code, *identity),
                            )
                    if unresolved:
                        connection.run("DELETE FROM window_completion WHERE code=?", (code,))
                    elif completed_day is not None:
                        cursor = connection.run(
                            "INSERT INTO window_completion VALUES (?,?,?) ON CONFLICT(code) DO UPDATE SET cutoff_date=excluded.cutoff_date,source=excluded.source WHERE cutoff_date<>excluded.cutoff_date OR source<>excluded.source",
                            (code, EncodeDate(completed_day), source_identity),
                        )
                        changed = changed or cursor.rowcount > 0
                for cache in (self, peer):
                    encoded_days = tuple(EncodeDate(day) for day in calendar[-cache.sessions :])
                    previous_days = tuple(
                        row[0]
                        for row in connection.run(
                            "SELECT trade_date FROM window_calendar WHERE profile=? ORDER BY trade_date",
                            (cache.profile,),
                        )
                    )
                    if encoded_days != previous_days:
                        changed = True
                        connection.run("DELETE FROM window_calendar WHERE profile=?", (cache.profile,))
                        connection.run_many(
                            "INSERT INTO window_calendar VALUES (?,?)", ((cache.profile, day) for day in encoded_days)
                        )
                if changed:
                    for cache in (self, peer):
                        cache._publish_identity(connection)
        except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
            reason = (
                "qfq_pair_database_busy"
                if isinstance(exc, sqlite3.OperationalError)
                and any(word in str(exc).lower() for word in ("locked", "busy"))
                else "qfq_pair_publication_failed"
            )
            raise QfqPairPublicationError((), (), 0, reason=reason) from exc
        return ((self.path.name,) if changed else ()), count

    def manifest(self) -> PublishedHistoryManifest | None:
        try:
            with self._read_connection() as connection:
                if connection is None:
                    return None
                with SqliteTransaction(connection):
                    return self._calculate_manifest(connection)
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise RuntimeError("qfq_manifest_unavailable") from exc

    def _calculate_manifest(self, connection: SqliteConnection) -> PublishedHistoryManifest | None:
        identities = tuple(
            (row[0], DecodeHash(row[1]), row[2], DecodeDate(row[3]).isoformat(), DecodeDate(row[4]).isoformat())
            for row in connection.run(
                "SELECT code,content_hash,source,start_date,end_date FROM window_binding WHERE profile=? ORDER BY code",
                (self.profile,),
            )
        )
        calendar = tuple(
            DecodeDate(row[0])
            for row in connection.run(
                "SELECT trade_date FROM window_calendar WHERE profile=? ORDER BY trade_date", (self.profile,)
            )
        )
        publication_id = self._publication_id(connection)
        if not identities:
            if publication_id is not None:
                raise RuntimeError("qfq_publication_identity_mismatch")
            return None
        if not calendar:
            raise RuntimeError("qfq_calendar_missing")
        if publication_id is None:
            raise RuntimeError("qfq_publication_identity_missing")
        digest = hashlib.sha256(
            json.dumps((identities, tuple(day.isoformat() for day in calendar)), separators=(",", ":")).encode("ascii")
        ).hexdigest()
        return PublishedHistoryManifest(
            digest, 1, calendar[-1], calendar, tuple(row[0] for row in identities), publication_id
        )

    def _publish_identity(self, connection: SqliteConnection) -> None:
        if connection.run("SELECT 1 FROM window_binding WHERE profile=? LIMIT 1", (self.profile,)).fetchone() is None:
            connection.run("DELETE FROM window_publication WHERE profile=?", (self.profile,))
        else:
            connection.run(
                "INSERT INTO window_publication VALUES (?,?) ON CONFLICT(profile) DO UPDATE SET publication_id=excluded.publication_id",
                (self.profile, uuid4().hex),
            )

    def _publication_id(self, connection: SqliteConnection) -> str | None:
        row = connection.run(
            "SELECT publication_id FROM window_publication WHERE profile=?", (self.profile,)
        ).fetchone()
        if row is None:
            return None
        identity = row[0]
        if (
            not isinstance(identity, str)
            or len(identity) != 32
            or any(character not in "0123456789abcdef" for character in identity)
        ):
            raise RuntimeError("qfq_publication_identity_invalid")
        return identity

    def _require_publication(self, connection: SqliteConnection, manifest: PublishedHistoryManifest) -> None:
        identity = self._publication_id(connection)
        if identity is None or identity != manifest.publication_id:
            raise RuntimeError("qfq_publication_changed")

    def iter_windows(
        self, manifest: PublishedHistoryManifest, *, sessions: int, check: Callable[[], None] | None = None
    ) -> Iterator[PublishedHistoryWindow]:
        allowed = frozenset(manifest.universe_codes)
        for start in range(0, len(manifest.universe_codes), 32):
            yield from self._read_windows(
                manifest, manifest.universe_codes[start : start + 32], sessions=sessions, allowed=allowed, check=check
            )

    def read_windows(
        self,
        manifest: PublishedHistoryManifest,
        codes: Sequence[str],
        *,
        sessions: int,
        check: Callable[[], None] | None = None,
    ) -> tuple[PublishedHistoryWindow, ...]:
        return self._read_windows(
            manifest, codes, sessions=sessions, allowed=frozenset(manifest.universe_codes), check=check
        )

    def _read_windows(
        self,
        manifest: PublishedHistoryManifest,
        codes: Sequence[str],
        *,
        sessions: int,
        allowed: frozenset[str],
        check: Callable[[], None] | None = None,
    ) -> tuple[PublishedHistoryWindow, ...]:
        if not 1 <= sessions <= self.sessions:
            raise ValueError("qfq_read_window_invalid")
        requested = tuple(sorted(set(codes) & allowed))
        for code in requested:
            _validate_code(code)
        first = manifest.calendar_dates[-min(sessions, len(manifest.calendar_dates))]
        windows: list[PublishedHistoryWindow] = []
        try:
            with self._read_connection() as connection:
                if connection is None:
                    raise RuntimeError("qfq_publication_changed")
                with WatchHistoryRead(connection, check):
                    self._require_publication(connection, manifest)
                    for start in range(0, len(requested), 32):
                        if check is not None:
                            check()
                        with SqliteTransaction(connection):
                            rows = self._batch_rows(connection, requested[start : start + 32])
                        windows.extend(
                            PublishedHistoryWindow(
                                code,
                                tuple(
                                    cell
                                    for day, values in cells
                                    if (cell := PriceCellFromColumns(code, day, values)).trade_date >= first
                                ),
                            )
                            for code, cells in rows
                        )
                    self._require_publication(connection, manifest)
        except (sqlite3.Error, OSError) as exc:
            raise RuntimeError("qfq_read_failed") from exc
        return tuple(windows)
