"""Shared structured prices with atomic profile identities and completion facts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import TypeAlias
from uuid import uuid4

from trader.download.application.update_qfq import QfqPairPublicationError, QfqWindowPort
from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow
from trader.download.domain.qfq_window import QFQ_WINDOWS, QfqWindowSnapshot, QfqWindowState
from trader.download.infra.price_columns import (
    PRICE_COLUMNS,
    PRICE_WINDOW_SQL,
    PriceCellFromColumns,
    PriceColumnValues,
    PriceValues,
)
from trader.infra.sqlite_connection import (
    CheckSqliteIntegrity,
    EnsureSqliteSchema,
    OpenSqliteConnection,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteTransaction,
)

_Rows: TypeAlias = tuple[tuple[str, PriceValues], ...]
_SELECT = ",".join(PRICE_COLUMNS)
_BAR_ROW_PARAMETERS = len(PRICE_COLUMNS) + 2
# 39 structured rows use 975 parameters, below SQLite's older 999-parameter limit.
_INSERT_ROWS_PER_STATEMENT = 39
_INSERT_ROW_PLACEHOLDERS = "(" + ",".join("?" for _ in range(_BAR_ROW_PARAMETERS)) + ")"
_SCHEMA = SqliteSchema(
    version=2,
    statements=(
        f"CREATE TABLE bars(code TEXT NOT NULL, day TEXT NOT NULL, {PRICE_WINDOW_SQL}, PRIMARY KEY(code,day)) WITHOUT ROWID",
        "CREATE TABLE identities(profile TEXT NOT NULL, code TEXT NOT NULL, digest TEXT NOT NULL, "
        "source TEXT NOT NULL, first_day TEXT NOT NULL, cutoff TEXT NOT NULL, "
        "PRIMARY KEY(profile,code)) WITHOUT ROWID",
        "CREATE TABLE calendars(profile TEXT NOT NULL, day TEXT NOT NULL, PRIMARY KEY(profile,day)) WITHOUT ROWID",
        "CREATE TABLE completions(code TEXT PRIMARY KEY, day TEXT NOT NULL, source TEXT NOT NULL) WITHOUT ROWID",
        "CREATE TABLE publications(profile TEXT PRIMARY KEY, identity TEXT NOT NULL) WITHOUT ROWID",
    ),
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
        payload = json.dumps(rows, separators=(",", ":"), allow_nan=False).encode("ascii")
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
        try:
            EnsureSqliteSchema(connection, _SCHEMA, allow_create=write)
            return connection
        except BaseException:
            connection.close()
            raise

    def verify_integrity(self) -> None:
        if self.path.is_file():
            with closing(self._connection(write=True)) as connection:
                CheckSqliteIntegrity(connection)

    def codes(self) -> frozenset[str]:
        if not self.path.exists():
            return frozenset()
        with closing(self._connection()) as connection:
            return frozenset(
                row[0] for row in connection.run("SELECT code FROM identities WHERE profile=?", (self.profile,))
            )

    def completed(self, day: date, code: str, source: str) -> bool:
        if not self.path.exists():
            return False
        with closing(self._connection()) as connection:
            return (
                connection.run(
                    "SELECT 1 FROM completions WHERE code=? AND day=? AND source=?", (code, day.isoformat(), source)
                ).fetchone()
                is not None
            )

    def _rows(self, connection: SqliteConnection, code: str) -> _Rows:
        identity = connection.run(
            "SELECT digest,first_day,cutoff FROM identities WHERE profile=? AND code=?", (self.profile, code)
        ).fetchone()
        if identity is None:
            return ()
        rows = tuple(
            (row[0], tuple(row[1:]))
            for row in connection.run(
                f"SELECT day,{_SELECT} FROM bars WHERE code=? AND day BETWEEN ? AND ? ORDER BY day",
                (code, identity[1], identity[2]),
            )
        )
        if len(rows) > self.sessions or _digest(rows) != identity[0]:
            raise RuntimeError("qfq_code_identity_mismatch")
        return rows

    def _batch_rows(self, connection: SqliteConnection, codes: tuple[str, ...]) -> tuple[tuple[str, _Rows], ...]:
        placeholders = ",".join("?" for _ in codes)
        identities = {
            str(code): str(digest)
            for code, digest in connection.run(
                f"SELECT code,digest FROM identities WHERE profile=? AND code IN ({placeholders})",
                (self.profile, *codes),
            )
        }
        grouped: dict[str, list[tuple[str, PriceValues]]] = {code: [] for code in identities}
        for code, day, *values in connection.run(
            f"SELECT bars.code,bars.day,{_SELECT} FROM identities AS identity "
            "JOIN bars ON bars.code=identity.code "
            "AND bars.day BETWEEN identity.first_day AND identity.cutoff "
            f"WHERE identity.profile=? AND identity.code IN ({placeholders}) ORDER BY bars.code,bars.day",
            (self.profile, *codes),
        ):
            grouped[str(code)].append((str(day), tuple(values)))
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
        if not self.path.exists():
            return tuple(PublishedHistoryWindow(code, ()) for code in requested)
        result: list[PublishedHistoryWindow] = []
        try:
            with closing(self._connection()) as connection:
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
        if not self.path.exists():
            return QfqWindowSnapshot(())
        states: list[QfqWindowState] = []
        with closing(self._connection()) as connection:
            identities = connection.run(
                "SELECT code,source FROM identities WHERE profile=? ORDER BY code", (self.profile,)
            ).fetchall()
            for code, source in identities:
                if code not in allowed_codes:
                    continue
                try:
                    with SqliteTransaction(connection):
                        rows = self._rows(connection, code)
                    states.append(QfqWindowState(code, source, tuple(date.fromisoformat(day) for day, _ in rows), True))
                except (RuntimeError, ValueError):
                    states.append(QfqWindowState(code, source, (), False))
        return QfqWindowSnapshot(tuple(states))

    def source_identity(self, code: str) -> str | None:
        if not self.path.exists():
            return None
        with closing(self._connection()) as connection:
            row = connection.run(
                "SELECT source FROM identities WHERE profile=? AND code=?", (self.profile, code)
            ).fetchone()
        return str(row[0]) if row else None

    def retain_codes(self, allowed_codes: frozenset[str]) -> tuple[tuple[str, ...], int]:
        for code in allowed_codes:
            _validate_code(code)
        excluded = self.codes() - allowed_codes
        if not excluded:
            return (), 0
        with closing(self._connection(write=True)) as connection, SqliteTransaction(connection, immediate=True):
            connection.run_many(
                "DELETE FROM identities WHERE profile=? AND code=?", ((self.profile, code) for code in excluded)
            )
            connection.run_many("DELETE FROM completions WHERE code=?", ((code,) for code in excluded))
            deleted = connection.run("DELETE FROM bars WHERE code NOT IN (SELECT code FROM identities)").rowcount
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
        if not self.path.is_file():
            return False
        with closing(self._connection()) as connection, SqliteTransaction(connection):
            for code, target in targets:
                actual = tuple(
                    (row[0], tuple(row[1:]))
                    for row in connection.run(f"SELECT day,{_SELECT} FROM bars WHERE code=? ORDER BY day", (code,))
                )
                if actual != target:
                    return False
            for cache in (self, peer):
                days = calendar[-cache.sessions :]
                if tuple(
                    row[0]
                    for row in connection.run(
                        "SELECT day FROM calendars WHERE profile=? ORDER BY day", (cache.profile,)
                    )
                ) != tuple(day.isoformat() for day in days):
                    return False
                for code, target in targets:
                    rows = tuple((day, values) for day, values in target if day >= days[0].isoformat())
                    identity = connection.run(
                        "SELECT digest,source,first_day,cutoff FROM identities WHERE profile=? AND code=?",
                        (cache.profile, code),
                    ).fetchone()
                    if identity is None or tuple(identity) != (
                        _digest(rows),
                        source,
                        days[0].isoformat(),
                        days[-1].isoformat(),
                    ):
                        return False
            if completed_day is not None:
                for code, _rows in targets:
                    if (
                        connection.run(
                            "SELECT 1 FROM completions WHERE code=? AND day=? AND source=?",
                            (code, completed_day.isoformat(), source),
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
                        (cell.trade_date.isoformat(), PriceColumnValues(cell))
                        for cell in window.cells
                        if cell.status == "complete"
                    ),
                )
                for window in windows
            )
            if self._matches_windows(peer, targets, source_identity, calendar, completed_day):
                return (), 0
            with closing(self._connection(write=True)) as connection, SqliteTransaction(connection, immediate=True):
                for code, target in targets:
                    old = tuple(
                        (row[0], tuple(row[1:]))
                        for row in connection.run(f"SELECT day,{_SELECT} FROM bars WHERE code=? ORDER BY day", (code,))
                    )
                    before, after = dict(old), dict(target)
                    count += sum(before.get(day) != after.get(day) for day in before.keys() | after.keys())
                    if old != target:
                        changed = True
                        connection.run_many(
                            "DELETE FROM bars WHERE code=? AND day=?",
                            ((code, day) for day in before.keys() - after.keys()),
                        )
                        inserts = tuple((day, values) for day, values in target if before.get(day) != values)
                        insert_command = "INSERT OR REPLACE INTO bars VALUES " if before else "INSERT INTO bars VALUES "
                        for start in range(0, len(inserts), _INSERT_ROWS_PER_STATEMENT):
                            batch = inserts[start : start + _INSERT_ROWS_PER_STATEMENT]
                            connection.run(
                                insert_command + ",".join(_INSERT_ROW_PLACEHOLDERS for _ in batch),
                                tuple(value for day, values in batch for value in (code, day, *values)),
                            )
                    for cache in (self, peer):
                        days = calendar[-cache.sessions :]
                        rows = tuple((day, values) for day, values in target if day >= days[0].isoformat())
                        identity = (_digest(rows), source_identity, days[0].isoformat(), days[-1].isoformat())
                        previous = connection.run(
                            "SELECT digest,source,first_day,cutoff FROM identities WHERE profile=? AND code=?",
                            (cache.profile, code),
                        ).fetchone()
                        if previous is None or tuple(previous) != identity:
                            changed = True
                            connection.run(
                                "INSERT OR REPLACE INTO identities VALUES (?,?,?,?,?,?)",
                                (cache.profile, code, *identity),
                            )
                    if completed_day is not None:
                        cursor = connection.run(
                            "INSERT INTO completions VALUES (?,?,?) ON CONFLICT(code) DO UPDATE SET day=excluded.day,source=excluded.source WHERE day<>excluded.day OR source<>excluded.source",
                            (code, completed_day.isoformat(), source_identity),
                        )
                        changed = changed or cursor.rowcount > 0
                for cache in (self, peer):
                    day_texts = tuple(day.isoformat() for day in calendar[-cache.sessions :])
                    previous_days = tuple(
                        row[0]
                        for row in connection.run(
                            "SELECT day FROM calendars WHERE profile=? ORDER BY day", (cache.profile,)
                        )
                    )
                    if day_texts != previous_days:
                        changed = True
                        connection.run("DELETE FROM calendars WHERE profile=?", (cache.profile,))
                        connection.run_many(
                            "INSERT INTO calendars VALUES (?,?)", ((cache.profile, day) for day in day_texts)
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
        if not self.path.exists():
            return None
        try:
            with closing(self._connection()) as connection, SqliteTransaction(connection):
                return self._calculate_manifest(connection)
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise RuntimeError("qfq_manifest_unavailable") from exc

    def _calculate_manifest(self, connection: SqliteConnection) -> PublishedHistoryManifest | None:
        identities = tuple(
            tuple(row)
            for row in connection.run(
                "SELECT code,digest,source,first_day,cutoff FROM identities WHERE profile=? ORDER BY code",
                (self.profile,),
            )
        )
        calendar = tuple(
            date.fromisoformat(row[0])
            for row in connection.run("SELECT day FROM calendars WHERE profile=? ORDER BY day", (self.profile,))
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
        if connection.run("SELECT 1 FROM identities WHERE profile=? LIMIT 1", (self.profile,)).fetchone() is None:
            connection.run("DELETE FROM publications WHERE profile=?", (self.profile,))
        else:
            connection.run(
                "INSERT INTO publications VALUES (?,?) ON CONFLICT(profile) DO UPDATE SET identity=excluded.identity",
                (self.profile, uuid4().hex),
            )

    def _publication_id(self, connection: SqliteConnection) -> str | None:
        row = connection.run("SELECT identity FROM publications WHERE profile=?", (self.profile,)).fetchone()
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

    def iter_windows(self, manifest: PublishedHistoryManifest, *, sessions: int) -> Iterator[PublishedHistoryWindow]:
        allowed = frozenset(manifest.universe_codes)
        for start in range(0, len(manifest.universe_codes), 32):
            yield from self._read_windows(
                manifest, manifest.universe_codes[start : start + 32], sessions=sessions, allowed=allowed
            )

    def read_windows(
        self, manifest: PublishedHistoryManifest, codes: Sequence[str], *, sessions: int
    ) -> tuple[PublishedHistoryWindow, ...]:
        return self._read_windows(manifest, codes, sessions=sessions, allowed=frozenset(manifest.universe_codes))

    def _read_windows(
        self, manifest: PublishedHistoryManifest, codes: Sequence[str], *, sessions: int, allowed: frozenset[str]
    ) -> tuple[PublishedHistoryWindow, ...]:
        if not 1 <= sessions <= self.sessions:
            raise ValueError("qfq_read_window_invalid")
        requested = tuple(sorted(set(codes) & allowed))
        for code in requested:
            _validate_code(code)
        first = manifest.calendar_dates[-min(sessions, len(manifest.calendar_dates))]
        windows: list[PublishedHistoryWindow] = []
        try:
            with closing(self._connection()) as connection:
                self._require_publication(connection, manifest)
                for start in range(0, len(requested), 32):
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
