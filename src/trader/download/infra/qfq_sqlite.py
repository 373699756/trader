"""Shared structured prices with atomic profile identities and completion facts."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import PublishSqliteFile, VerifiedSqlitePublication, VerifySqliteFile
from trader.download.domain.download_failure import DownloadFailureConfiguration, DownloadFailureRecord
from trader.download.infra.download_failure_diagnostics import FAILURE_DIAGNOSTICS_SQL, WriteDownloadFailure

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Literal, TypeAlias
from uuid import uuid4

from trader.download.application.update_qfq import QfqPairPublicationError, QfqWindowPort
from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow, ValidatePublishedHistoryCodes
from trader.download.domain.price_repair import PriceIssue
from trader.download.infra.price_repair_attempts import PRICE_REPAIR_ATTEMPT_SQL, PRICE_SOURCE_CHECK_SQL, PriceRepairAttempts, RecordTencentCheck
from trader.download.infra.published_history_codec import EncodePublishedHistoryCell, DecodePublishedHistoryCellPayload
from trader.download.domain.qfq_window import QfqWindowConfiguration, QfqWindowSnapshot, QfqWindowState
from trader.download.domain.qfq_planning import QfqNonTradingFact, QfqPlanningSnapshot, QfqPlanningState
from trader.download.infra.qfq_nontrading import (
    ObservedNonTradingFacts, ReadNonTradingFacts, ReplaceNonTradingFacts,
)
from trader.download.domain.price_review import PriceReviewBudgetExhausted, PriceReviewRequestBudget
from trader.download.infra.qfq_review_guard import GuardQfqReviewSqlite
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
_POPULATION_SQL = (
    "CREATE TABLE IF NOT EXISTS window_population(profile TEXT NOT NULL, code TEXT NOT NULL, "
    "PRIMARY KEY(profile,code)) WITHOUT ROWID"
)
_REPAIR_SQL = (
    FAILURE_DIAGNOSTICS_SQL,
    PRICE_SOURCE_CHECK_SQL,
    PRICE_REPAIR_ATTEMPT_SQL.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1),
    "CREATE TABLE IF NOT EXISTS price_issue(code TEXT NOT NULL, trade_date INTEGER NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(code,trade_date)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS price_candidate(code TEXT NOT NULL, trade_date INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(code,trade_date)) WITHOUT ROWID",
    "CREATE TABLE price_nontrading(code TEXT NOT NULL,trade_date INTEGER NOT NULL,kind TEXT NOT NULL,"
    "checked_on INTEGER,source TEXT,PRIMARY KEY(code,trade_date)) WITHOUT ROWID",
)
_SCHEMA = SqliteSchema(
    statements=(
        f"CREATE TABLE daily_price(code TEXT NOT NULL, trade_date INTEGER NOT NULL, {PRICE_WINDOW_SQL}, PRIMARY KEY(code,trade_date)) WITHOUT ROWID",
        "CREATE TABLE window_binding(profile TEXT NOT NULL, code TEXT NOT NULL, content_hash BLOB NOT NULL CHECK(length(content_hash)=32), "
        "source TEXT NOT NULL, start_date INTEGER NOT NULL, end_date INTEGER NOT NULL, "
        "PRIMARY KEY(profile,code)) WITHOUT ROWID",
        "CREATE TABLE window_calendar(profile TEXT NOT NULL, trade_date INTEGER NOT NULL, PRIMARY KEY(profile,trade_date)) WITHOUT ROWID",
        "CREATE TABLE window_completion(code TEXT PRIMARY KEY, cutoff_date INTEGER NOT NULL, source TEXT NOT NULL) WITHOUT ROWID",
        "CREATE TABLE window_publication(profile TEXT PRIMARY KEY, publication_id TEXT NOT NULL) WITHOUT ROWID",
        _POPULATION_SQL,
        *_REPAIR_SQL,
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
        canonical = tuple((DecodeDate(day).isoformat(), DecodePriceValues(values)) for day, values in rows)
        payload = json.dumps(canonical, separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("qfq_code_identity_mismatch") from exc
    return hashlib.sha256(payload).hexdigest()


class SQLiteQfqWindowCache:
    """Profile view; construction and absent reads have no filesystem side effects."""

    def __init__(self, path: Path, profile: Literal["v2", "v3"], *, sqlite_policy: SqliteIoPolicy) -> None:
        self._sqlite_policy = sqlite_policy
        if profile not in ("v2", "v3"):
            raise ValueError("qfq_profile_invalid")
        self.path = path
        self.profile = profile
        self._file_verification: VerifiedSqlitePublication | None = None
        self._publication_depth = 0
        self._file_dirty = False

    def _connection(self, *, write: bool = False, review_budget: PriceReviewRequestBudget | None = None) -> SqliteConnection:
        policy = self._sqlite_policy
        if review_budget is not None:
            review_budget.check_deadline()
            # OpenSqliteConnection takes the effective timeout from the policy,
            # overriding SqliteConnectionOptions. Keep ordinary I/O unchanged.
            policy = replace(policy, connection_timeout_seconds=0.001)
        if self.path.is_symlink() or self.path.is_dir():
            raise RuntimeError("qfq_path_unsafe")
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = OpenSqliteConnection(
            self.path,
            SqliteConnectionOptions(
                read_only=not write,
                journal_mode="WAL" if write else None,
                timeout_seconds=0.001 if review_budget is not None else 5.0,
            ), sqlite_policy=policy,
        )
        if review_budget is not None:
            try:
                review_budget.check_deadline()
            except PriceReviewBudgetExhausted:
                connection.close()
                raise
        return connection

    @contextmanager
    def _read_connection(self, *, review_budget: PriceReviewRequestBudget | None = None) -> Iterator[SqliteConnection | None]:
        if not self.path.exists():
            yield None
            return
        with closing(self._connection(review_budget=review_budget)) as connection, GuardQfqReviewSqlite(connection, review_budget):
            # A rolled-back first publication has no schema or visible windows.
            # Populated incompatible databases require a fresh download.
            if not HasSqliteSchema(connection, _SCHEMA):
                yield None
                return
            if self._publication_depth == 0:
                self.verify_integrity()
            yield connection

    @contextmanager
    def _write_connection(self, *, review_budget: PriceReviewRequestBudget | None = None) -> Iterator[SqliteConnection]:
        if review_budget is not None:
            review_budget.check_deadline()
        if self._publication_depth == 0 and self.path.is_file():
            self.verify_integrity()
        with closing(self._connection(write=True, review_budget=review_budget)) as connection, SqliteTransaction(connection, immediate=True):
            with GuardQfqReviewSqlite(connection, review_budget):
                EnsureSqliteSchema(connection, _SCHEMA, allow_create=True)
                yield connection
        self._mark_publication()

    @contextmanager
    def publication(self, peer: QfqWindowPort) -> Iterator[None]:
        """Batch committed writes into one physical file fingerprint."""
        if not isinstance(peer, SQLiteQfqWindowCache) or peer.path != self.path or peer is self:
            raise ValueError("qfq_pair_publication_invalid")
        if self._publication_depth == 0 and self.path.is_file():
            self.verify_integrity()
        self._publication_depth += 1
        peer._publication_depth += 1
        try:
            yield
        finally:
            self._publication_depth -= 1
            peer._publication_depth -= 1
            if self._publication_depth == 0 and self.path.is_file() and (self._file_dirty or peer._file_dirty):
                self.publish_file()
                peer._file_dirty = False
                peer._file_verification = None

    def _mark_publication(self) -> None:
        self._file_dirty = True
        if self._publication_depth == 0:
            self.publish_file()

    def RecordDownloadFailure(self, record: DownloadFailureRecord, *, configuration: DownloadFailureConfiguration) -> None:
        with self._write_connection() as connection:
            WriteDownloadFailure(connection, record, configuration)

    def price_issues(
        self, codes: frozenset[str] | None = None, dates: tuple[date, ...] | None = None
    ) -> tuple[PriceIssue, ...]:
        with self._read_connection() as connection:
            if connection is None:
                return ()
            grouped: dict[str, list[date]] = {}
            reasons: dict[str, str] = {}
            if (codes is not None and not codes) or (dates is not None and not dates):
                return ()
            ordered_codes = tuple(sorted(codes)) if codes is not None else ()
            batches = (None,) if codes is None else tuple(ordered_codes[start:start + 128] for start in range(0, len(ordered_codes), 128))
            for batch in batches:
                conditions: list[str] = []
                parameters: list[str | int] = []
                if batch is not None:
                    conditions.append("code IN (" + ",".join("?" for _ in batch) + ")")
                    parameters.extend(batch)
                if dates is not None:
                    conditions.append("trade_date BETWEEN ? AND ?")
                    parameters.extend((EncodeDate(dates[0]), EncodeDate(dates[-1])))
                where = " WHERE " + " AND ".join(conditions) if conditions else ""
                for code, day, reason in connection.run("SELECT code,trade_date,reason FROM price_issue" + where + " ORDER BY code,trade_date", tuple(parameters)):
                    decoded = DecodeDate(day)
                    if dates is None or decoded in dates:
                        grouped.setdefault(code, []).append(decoded)
                        reasons[code] = reason
        return tuple(PriceIssue(code, tuple(days), reasons[code]) for code, days in sorted(grouped.items()))

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
            if connection is not None:
                binding = connection.run("SELECT source,start_date,end_date FROM window_binding WHERE profile=? AND code=?", (self.profile, code)).fetchone()
                if binding is not None:
                    days.update(fact.trade_date for fact in ReadNonTradingFacts(connection, (code,)) if fact.kind != "legacy_unverified" and fact.source == binding[0] and binding[1] <= EncodeDate(fact.trade_date) <= binding[2])
        return tuple(sorted(days))

    def repair_reference(self, code: str) -> PublishedHistoryWindow:
        cells = {cell.trade_date: cell for cell in self.read_code(code).cells}
        with self._read_connection() as connection:
            if connection is not None:
                for day, payload in connection.run("SELECT trade_date,payload FROM price_candidate WHERE code=?", (code,)):
                    cell = DecodePublishedHistoryCellPayload(code, DecodeDate(day).isoformat(), payload)
                    if cell.unadjusted is not None:
                        cells[cell.trade_date] = cell
        return PublishedHistoryWindow(code, tuple(cells[day] for day in sorted(cells)))

    def reserve_repair(self, issue: PriceIssue, batch: str, *, explicit: bool) -> bool:
        with self._write_connection() as connection:
            return bool(PriceRepairAttempts(connection).reserve(issue.code, issue.dates, batch, explicit=explicit))

    def fail_repair(self, code: str, batch: str, reason: str) -> None:
        with self._write_connection() as connection:
            PriceRepairAttempts(connection).fail(code, batch, reason.lower())
            connection.run("UPDATE price_issue SET reason=? WHERE code=?", (reason, code))

    def verify_integrity(self) -> None:
        if self.path.is_file():
            if not Path(f"{self.path}.sha256").is_file():
                with closing(self._connection()) as connection:
                    if not HasSqliteSchema(connection, _SCHEMA):
                        return
            self._file_verification = VerifySqliteFile(
                self.path,
                sqlite_policy=self._sqlite_policy,
                previous=self._file_verification,
            )

    def publish_file(self) -> None:
        if self.path.is_file():
            PublishSqliteFile(self.path, sqlite_policy=self._sqlite_policy)
            self._file_verification = None
            self._file_dirty = False

    def codes(self) -> frozenset[str]:
        with self._read_connection() as connection:
            if connection is None:
                return frozenset()
            return frozenset(
                row[0] for row in connection.run(
                    "SELECT code FROM window_binding WHERE profile=? AND code IN "
                    "(SELECT code FROM window_population WHERE profile=?)", (self.profile, self.profile),
                )
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

    def _session_limit(self, connection: SqliteConnection) -> int:
        value = connection.run(
            "SELECT COUNT(*) FROM window_calendar WHERE profile=?", (self.profile,)
        ).require_one()[0]
        if not isinstance(value, int) or value < 1:
            raise RuntimeError("qfq_calendar_missing")
        return value

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
        session_limit = self._session_limit(connection) if identities else 0
        for code in codes:
            rows = tuple(grouped.get(code, ()))
            digest = identities.get(code)
            if digest is not None and len(rows) > session_limit:
                raise RuntimeError("qfq_code_identity_mismatch")
            result.append((code, rows))
        return tuple(result)

    def read_code(self, code: str, *, review_budget: PriceReviewRequestBudget | None = None) -> PublishedHistoryWindow:
        return self.read_codes((code,), review_budget=review_budget)[0]

    def read_codes(self, codes: Sequence[str], *, review_budget: PriceReviewRequestBudget | None = None) -> tuple[PublishedHistoryWindow, ...]:
        requested = tuple(sorted(set(codes)))
        for code in requested:
            _validate_code(code)
        result: list[PublishedHistoryWindow] = []
        try:
            with self._read_connection(review_budget=review_budget) as connection:
                if connection is None:
                    return tuple(PublishedHistoryWindow(code, ()) for code in requested)
                for start in range(0, len(requested), 32):
                    if review_budget is not None:
                        review_budget.check_deadline()
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
                "SELECT code,source,start_date,end_date FROM window_binding WHERE profile=? ORDER BY code",
                (self.profile,),
            ).fetchall()
            for code, source, start, end in identities:
                if code not in allowed_codes:
                    continue
                try:
                    start_date, end_date = DecodeDate(start), DecodeDate(end)
                    if start_date > end_date:
                        raise RuntimeError("qfq_code_identity_mismatch")
                    dates = tuple(
                        DecodeDate(row[0])
                        for row in connection.run(
                            "SELECT trade_date FROM daily_price WHERE code=? AND trade_date BETWEEN ? AND ? "
                            "ORDER BY trade_date", (code, start, end),
                        )
                    )
                    if len(dates) > self._session_limit(connection):
                        raise RuntimeError("qfq_code_identity_mismatch")
                    states.append(QfqWindowState(
                        code, source, dates, True, start_date, end_date
                    ))
                except (RuntimeError, ValueError):
                    states.append(QfqWindowState(code, source, (), False, None, None))
        return QfqWindowSnapshot(tuple(states))

    def _inspect_windows_batched(self, connection: SqliteConnection, allowed_codes: frozenset[str]) -> QfqWindowSnapshot:
        states: list[QfqWindowState] = []
        session_limit: int | None = None
        codes = tuple(sorted(allowed_codes))
        for start in range(0, len(codes), 128):
            batch = codes[start:start + 128]
            placeholders = ",".join("?" for _ in batch)
            identities = connection.run(
                f"SELECT code,source,start_date,end_date FROM window_binding WHERE profile=? AND code IN ({placeholders}) ORDER BY code",
                (self.profile, *batch),
            ).fetchall()
            if identities and session_limit is None:
                session_limit = self._session_limit(connection)
            grouped: dict[str, list[date]] = {}
            invalid_codes: set[str] = set()
            for code, day in connection.run(
                "SELECT prices.code,prices.trade_date FROM window_binding AS binding JOIN daily_price AS prices "
                "ON prices.code=binding.code AND prices.trade_date BETWEEN binding.start_date AND binding.end_date "
                f"WHERE binding.profile=? AND binding.code IN ({placeholders}) ORDER BY prices.code,prices.trade_date",
                (self.profile, *batch),
            ):
                try:
                    grouped.setdefault(code, []).append(DecodeDate(day))
                except (RuntimeError, ValueError):
                    invalid_codes.add(code)
            for code, source, first, last in identities:
                try:
                    first_day, last_day = DecodeDate(first), DecodeDate(last)
                    days = tuple(grouped.get(code, ()))
                    if first_day > last_day or session_limit is None or len(days) > session_limit or code in invalid_codes:
                        raise RuntimeError("qfq_code_identity_mismatch")
                    states.append(QfqWindowState(code, source, days, True, first_day, last_day))
                except (RuntimeError, ValueError):
                    states.append(QfqWindowState(code, source, (), False, None, None))
        return QfqWindowSnapshot(tuple(states))

    def planning_snapshot(self, allowed_codes: frozenset[str], *, batched: bool = False) -> QfqPlanningSnapshot:
        """Read provenance and receipts together; the batch query remains opt-in."""
        windows = None if batched else self.inspect_windows(allowed_codes)
        with self._read_connection() as connection:
            if connection is None:
                return QfqPlanningSnapshot(QfqWindowSnapshot(()), ())
            with SqliteTransaction(connection):
                if windows is None:
                    windows = self._inspect_windows_batched(connection, allowed_codes)
                return QfqPlanningSnapshot(windows, self._planning_metadata(connection, tuple(state.code for state in windows.states)))

    def review_states(self, allowed_codes: frozenset[str], *, review_budget: PriceReviewRequestBudget | None = None) -> tuple[QfqPlanningState, ...]:
        """Review receipts without rereading every local price date a second time."""
        try:
            with self._read_connection(review_budget=review_budget) as connection:
                if connection is None:
                    return ()
                with SqliteTransaction(connection):
                    return self._planning_metadata(connection, tuple(sorted(allowed_codes)), review_budget=review_budget)
        except sqlite3.Error as exc:
            raise RuntimeError("qfq_price_review_read_failed") from exc

    def _planning_metadata(self, connection: SqliteConnection, codes: tuple[str, ...], *, review_budget: PriceReviewRequestBudget | None = None) -> tuple[QfqPlanningState, ...]:
        metadata: list[QfqPlanningState] = []
        facts: dict[str, list[QfqNonTradingFact]] = {}
        for fact in ReadNonTradingFacts(connection, codes, review_budget=review_budget):
            facts.setdefault(fact.code, []).append(fact)
        for start in range(0, len(codes), 128):
            if review_budget is not None:
                review_budget.check_deadline()
            batch = codes[start:start + 128]
            placeholders = ",".join("?" for _ in batch)
            for code, source, completed_day, completed_source, checked_on in connection.run(
                "SELECT binding.code,binding.source,completion.cutoff_date,completion.source,checks.trade_date FROM window_binding AS binding "
                "LEFT JOIN window_completion AS completion ON completion.code=binding.code "
                "LEFT JOIN price_tencent_check AS checks ON checks.code=binding.code"
                f" WHERE binding.profile=? AND binding.code IN ({placeholders}) ORDER BY binding.code",
                (self.profile, *batch),
            ):
                if review_budget is not None:
                    review_budget.check_deadline()
                metadata.append(QfqPlanningState(
                    code, source, DecodeDate(completed_day) if completed_day is not None else None, completed_source,
                    DecodeDate(checked_on) if checked_on is not None else None, tuple(facts.get(code, ())),
                ))
        return tuple(metadata)

    def _has_price_issues(self, codes: tuple[str, ...], calendar: tuple[date, ...]) -> bool:
        with self._read_connection() as connection:
            if connection is None:
                return False
            for start in range(0, len(codes), 128):
                batch = codes[start:start + 128]
                placeholders = ",".join("?" for _ in batch)
                if connection.run(f"SELECT 1 FROM price_issue WHERE code IN ({placeholders}) AND trade_date BETWEEN ? AND ? LIMIT 1", (*batch, EncodeDate(calendar[0]), EncodeDate(calendar[-1]))).fetchone():
                    return True
        return False

    def _has_nontrading(self, codes: tuple[str, ...]) -> bool:
        with self._read_connection() as connection:
            return bool(connection is not None and ReadNonTradingFacts(connection, codes))

    def source_identity(self, code: str) -> str | None:
        with self._read_connection() as connection:
            if connection is None:
                return None
            row = connection.run(
                "SELECT source FROM window_binding WHERE profile=? AND code=?", (self.profile, code)
            ).fetchone()
        return str(row[0]) if row else None

    def select_published_codes(
        self, peer: QfqWindowPort, allowed_codes: frozenset[str]
    ) -> tuple[tuple[str, ...], int]:
        """Declare current publication members without deleting retained window facts."""
        if (
            not isinstance(peer, SQLiteQfqWindowCache)
            or self.path != peer.path
            or {self.profile, peer.profile} != {"v2", "v3"}
        ):
            raise ValueError("qfq_pair_layout_invalid")
        for code in allowed_codes:
            _validate_code(code)
        changes: list[tuple[SQLiteQfqWindowCache, tuple[str, ...]]] = []
        with self._read_connection() as connection:
            if connection is None:
                return (), 0
            with SqliteTransaction(connection):
                for cache in (self, peer):
                    desired = tuple(
                        row[0] for row in connection.run(
                            "SELECT code FROM window_binding WHERE profile=? ORDER BY code", (cache.profile,),
                        ) if row[0] in allowed_codes
                    )
                    previous = tuple(
                        row[0] for row in connection.run(
                            "SELECT code FROM window_population WHERE profile=? ORDER BY code", (cache.profile,),
                        )
                    )
                    if previous != desired or bool(cache._publication_id(connection)) != bool(desired):
                        changes.append((cache, desired))
        if not changes:
            return (), 0
        with self._write_connection() as connection:
            for cache, desired in changes:
                connection.run("DELETE FROM window_population WHERE profile=?", (cache.profile,))
                connection.run_many(
                    "INSERT INTO window_population VALUES(?,?)", ((cache.profile, code) for code in desired),
                )
                cache._publish_identity(connection)
        return (self.path.name,), 0

    def _matches_windows(
        self,
        peer: SQLiteQfqWindowCache,
        targets: tuple[tuple[str, _Rows], ...],
        source: str,
        configuration: QfqWindowConfiguration,
        calendar: tuple[date, ...],
        completed_day: date | None,
    ) -> bool:
        with self._read_connection() as connection:
            if connection is None:
                return False
            with SqliteTransaction(connection):
                # File verification owns persisted content integrity. Compare the
                # incoming windows to their committed identities without rereading prices.
                for cache in (self, peer):
                    days = calendar[-configuration.sessions_for(cache.profile) :]
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
                        if connection.run(
                            "SELECT 1 FROM window_population WHERE profile=? AND code=?", (cache.profile, code),
                        ).fetchone() is None:
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
        configuration: QfqWindowConfiguration,
        *,
        calendar: tuple[date, ...],
        completed_day: date | None,
        nontrading_facts: tuple[QfqNonTradingFact, ...] | None = None,
        review_budget: PriceReviewRequestBudget | None = None,
    ) -> tuple[tuple[str, ...], int]:
        if (
            not isinstance(peer, SQLiteQfqWindowCache)
            or self.path != peer.path
            or {self.profile, peer.profile} != {"v2", "v3"}
        ):
            raise ValueError("qfq_pair_layout_invalid")
        if (
            not calendar
            or calendar != tuple(sorted(set(calendar)))
            or len(calendar) > configuration.maximum_sessions
        ):
            raise ValueError("qfq_calendar_invalid")
        if completed_day is not None and completed_day != calendar[-1]:
            raise ValueError("qfq_completion_day_invalid")
        allowed_dates = frozenset(calendar)
        for window in windows:
            if review_budget is not None:
                review_budget.check_deadline()
            _validate_code(window.code)
            if any(cell.trade_date not in allowed_dates for cell in window.cells):
                raise ValueError("qfq_window_outside_calendar")
        if nontrading_facts is not None:
            observed = {(window.code, cell.trade_date): cell.status for window in windows for cell in window.cells if cell.status in ("supplier_marked_suspended", "tencent_inferred_suspended")}
            if len({(fact.code, fact.trade_date) for fact in nontrading_facts}) != len(nontrading_facts) or set(observed) != {(fact.code, fact.trade_date) for fact in nontrading_facts}:
                raise ValueError("qfq_nontrading_fact_identity_conflict")
            if any(fact.source != source_identity or fact.kind != ("confirmed" if observed[(fact.code, fact.trade_date)] == "supplier_marked_suspended" else "inferred") for fact in nontrading_facts):
                raise ValueError("qfq_nontrading_fact_provenance_conflict")
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
            # An explicit observation must refresh its receipt even when prices
            # are unchanged. Unknown seed facts never gain source provenance.
            has_nontrading = any(cell.status in ("supplier_marked_suspended", "tencent_inferred_suspended") for window in windows for cell in window.cells)
            target_codes = tuple(window.code for window in windows)
            if review_budget is None and not has_nontrading and not self._has_nontrading(target_codes) and not self._has_price_issues(target_codes, calendar) and self._matches_windows(
                peer, targets, source_identity, configuration, calendar, completed_day
            ):
                return (), 0
            with self._write_connection(review_budget=review_budget) as connection:
                for code, target in targets:
                    if review_budget is not None:
                        review_budget.check_deadline()
                    RecordTencentCheck(connection, code, calendar[-1], initial=source_identity.startswith("baostock:"))
                    window = next(item for item in windows if item.code == code)
                    obtained = tuple(cell.trade_date for cell in window.cells if cell.status in ("complete", "supplier_marked_suspended", "tencent_inferred_suspended"))
                    facts = tuple(fact for fact in nontrading_facts if fact.code == code) if nontrading_facts is not None else ObservedNonTradingFacts(window, source_identity, None)
                    changed = ReplaceNonTradingFacts(connection, code, facts) or changed
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
                        days = calendar[-configuration.sessions_for(cache.profile) :]
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
                        member = connection.run(
                            "INSERT OR IGNORE INTO window_population VALUES(?,?)", (cache.profile, code),
                        )
                        changed = changed or member.rowcount > 0
                    if unresolved:
                        connection.run("DELETE FROM window_completion WHERE code=?", (code,))
                    elif completed_day is not None:
                        cursor = connection.run(
                            "INSERT INTO window_completion VALUES (?,?,?) ON CONFLICT(code) DO UPDATE SET cutoff_date=excluded.cutoff_date,source=excluded.source WHERE cutoff_date<>excluded.cutoff_date OR source<>excluded.source",
                            (code, EncodeDate(completed_day), source_identity),
                        )
                        changed = changed or cursor.rowcount > 0
                for cache in (self, peer):
                    encoded_days = tuple(
                        EncodeDate(day)
                        for day in calendar[-configuration.sessions_for(cache.profile) :]
                    )
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
        except PriceReviewBudgetExhausted:
            raise
        except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
            reason = (
                "qfq_pair_database_busy"
                if isinstance(exc, sqlite3.OperationalError)
                and any(word in str(exc).lower() for word in ("locked", "busy"))
                else "qfq_pair_publication_failed"
            )
            raise QfqPairPublicationError((), (), 0, reason=reason) from exc
        if changed or not Path(f"{self.path}.sha256").is_file():
            self._mark_publication()
        return ((self.path.name,) if changed else ()), count

    def manifest(self) -> PublishedHistoryManifest | None:
        self.verify_integrity()
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
                "SELECT code,content_hash,source,start_date,end_date FROM window_binding WHERE profile=? "
                "AND code IN (SELECT code FROM window_population WHERE profile=?) ORDER BY code",
                (self.profile, self.profile),
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
        if connection.run(
            "SELECT 1 FROM window_binding WHERE profile=? AND code IN "
            "(SELECT code FROM window_population WHERE profile=?) LIMIT 1", (self.profile, self.profile),
        ).fetchone() is None:
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
        self, manifest: PublishedHistoryManifest, *, sessions: int,
        codes: frozenset[str] | None = None, check: Callable[[], None] | None = None,
    ) -> Iterator[PublishedHistoryWindow]:
        ValidatePublishedHistoryCodes(codes)
        if check is not None:
            check()
        if not 1 <= sessions <= len(manifest.calendar_dates):
            raise ValueError("qfq_read_window_invalid")
        allowed = frozenset(manifest.universe_codes)
        requested = manifest.universe_codes if codes is None else tuple(sorted(codes & allowed))
        if not requested:
            with self._read_connection() as connection:
                if connection is None:
                    raise RuntimeError("qfq_publication_changed")
                self._require_publication(connection, manifest)
            return
        for start in range(0, len(requested), 32):
            yield from self._read_windows(
                manifest, requested[start : start + 32], sessions=sessions, allowed=allowed, check=check
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

    def read_range(
        self,
        manifest: PublishedHistoryManifest,
        codes: Sequence[str],
        start: date,
        end: date,
        *,
        check: Callable[[], None] | None = None,
    ) -> tuple[PublishedHistoryWindow, ...]:
        if start > end or start < manifest.calendar_dates[0] or end > manifest.data_cutoff:
            raise ValueError("qfq_read_window_invalid")
        return tuple(
            PublishedHistoryWindow(window.code, tuple(cell for cell in window.cells if start <= cell.trade_date <= end))
            for window in self.read_windows(manifest, codes, sessions=len(manifest.calendar_dates), check=check)
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
        self.verify_integrity()
        if not 1 <= sessions <= len(manifest.calendar_dates):
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
