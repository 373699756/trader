"""Current typed source observations; independent from request and publication logs."""

from collections.abc import Iterator, Callable
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.download.domain.company_annual_income import CompanyAnnualIncome
from trader.download.domain.company_evidence import CompanyInputValidationFailure, CompanyRatio, StructuredCompanyPage, StructuredCompanyQuery
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema, SqliteConnection, SqliteConnectionOptions, SqliteSchema, SqliteSession, SqliteTransaction,
)
from trader.infra.sqlite_policy import SqliteIoPolicy


_SCHEMA = SqliteSchema((
    """CREATE TABLE company_recovered_input (
      scope TEXT NOT NULL,code TEXT NOT NULL,source TEXT NOT NULL,source_key TEXT NOT NULL,
      record_hash TEXT NOT NULL,body_hash BLOB NOT NULL CHECK(length(body_hash)=32),prepared INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(scope,code,source,source_key,record_hash,body_hash)
    ) WITHOUT ROWID""",
    "CREATE TABLE company_pledge_day(scope TEXT PRIMARY KEY,day TEXT NOT NULL) WITHOUT ROWID",
    """CREATE TABLE company_source_page (
      scope TEXT NOT NULL,component TEXT NOT NULL,page INTEGER NOT NULL,pages INTEGER NOT NULL,
      total INTEGER NOT NULL,raw_count INTEGER NOT NULL,decoded_hash TEXT NOT NULL,
      PRIMARY KEY(scope,component,page)
    ) WITHOUT ROWID""",
    """CREATE TABLE company_ratio_observation (
      scope TEXT NOT NULL,component TEXT NOT NULL,code TEXT NOT NULL,day TEXT NOT NULL,ratio REAL NOT NULL,
      PRIMARY KEY(scope,component,code,day)
    ) WITHOUT ROWID""",
    """CREATE TABLE company_source_task (
      scope TEXT NOT NULL,family TEXT NOT NULL,task TEXT NOT NULL,accepted INTEGER NOT NULL,input_hash TEXT NOT NULL,
      PRIMARY KEY(scope,family,task)
    ) WITHOUT ROWID""",
    """CREATE TABLE company_annual_income_observation (
      scope TEXT NOT NULL,code TEXT NOT NULL,report_date TEXT NOT NULL,disclosed_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,observed_at TEXT NOT NULL,currency TEXT NOT NULL,revenue REAL,record_hash TEXT NOT NULL,
      PRIMARY KEY(scope,code,report_date,disclosed_at,updated_at)
    ) WITHOUT ROWID""",
))


@dataclass(frozen=True, slots=True)
class CompanyRecoveredInput:
    code: str
    source: str
    source_key: str
    record_hash: str
    body_hash: bytes


class SQLiteCompanySyncInputStore:
    def __init__(self, path: Path, *, sqlite_policy: SqliteIoPolicy, io_budget: CompanySyncIoBudget | None = None) -> None:
        self.path, self._sqlite_policy = path, sqlite_policy
        self._io_budget = io_budget

    @contextmanager
    def _session(self, path: Path, options: SqliteConnectionOptions | None = None, *,
        sqlite_policy: SqliteIoPolicy, cancel_requested: Callable[[], bool] | None = None) -> Iterator[SqliteConnection]:
        actual_options = SqliteConnectionOptions() if options is None else options
        if self._io_budget is not None:
            with self._io_budget.session(path, actual_options, sqlite_policy=sqlite_policy, cancel_requested=cancel_requested) as connection:
                yield connection
        else:
            with SqliteSession(path, actual_options, sqlite_policy=sqlite_policy, cancel_requested=cancel_requested) as connection:
                yield connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection:
            EnsureSqliteSchema(connection, _SCHEMA, allow_create=True, exact=True)

    def validate_existing(self) -> None:
        if self.path.exists():
            with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
                EnsureSqliteSchema(connection, _SCHEMA, exact=True)

    def ratio_cursor(self, scope: str, query: StructuredCompanyQuery, cutoff: datetime) -> StructuredCompanyQuery | None:
        _query(query, cutoff)
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            row = connection.run("SELECT MAX(page),pages,total,SUM(raw_count) FROM company_source_page WHERE scope=? AND component=?", (scope, query.component)).require_one()
        if row[0] is None:
            return StructuredCompanyQuery(query.component, query.start, query.end)
        if row[0] == row[1]:
            if row[2] != row[3]:
                raise ValueError("company_ratio_source_count_mismatch")
            return None
        return StructuredCompanyQuery(query.component, query.start, query.end, row[0] + 1, row[2], row[3])

    def pledge_day(self, scope: str) -> date | None:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            row = connection.run("SELECT day FROM company_pledge_day WHERE scope=?", (scope,)).fetchone()
            return date.fromisoformat(row[0]) if row is not None else None

    def freeze_pledge_day(self, scope: str, day: date, cutoff: datetime) -> None:
        today = cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if not today - timedelta(days=365) <= day <= today:
            raise CompanyInputValidationFailure("company_pledge_snapshot_day_outside_window")
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            old = connection.run("SELECT day FROM company_pledge_day WHERE scope=?", (scope,)).fetchone()
            if old is not None and old[0] != day.isoformat():
                raise ValueError("company_pledge_snapshot_day_changed")
            connection.run("INSERT OR IGNORE INTO company_pledge_day VALUES(?,?)", (scope, day.isoformat()))

    def accept_ratios(self, scope: str, query: StructuredCompanyQuery, page: StructuredCompanyPage, cutoff: datetime, *, eligible_codes: tuple[str, ...] | None = None) -> None:
        _query(query, cutoff)
        if (page.component != query.component or page.page != query.page or page.financials
            or query.expected_rows is not None and page.total != query.expected_rows
            or any(not query.start <= item.day <= query.end for item in page.ratios)):
            raise ValueError("company_ratio_source_page_mismatch")
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            row = connection.run("SELECT MAX(page),pages,total FROM company_source_page WHERE scope=? AND component=?", (scope, query.component)).require_one()
            if page.page != (row[0] or 0) + 1 or row[0] is not None and (page.pages, page.total) != (row[1], row[2]):
                raise ValueError("company_ratio_source_pagination_changed")
            for item in page.ratios:
                if eligible_codes is not None and item.code not in eligible_codes:
                    continue
                if connection.run("SELECT 1 FROM company_ratio_observation WHERE scope=? AND component=? AND code=? AND day=?", (scope, query.component, item.code, item.day.isoformat())).fetchone() is not None:
                    raise ValueError("company_ratio_source_duplicate_member")
                connection.run("INSERT INTO company_ratio_observation VALUES(?,?,?,?,?)", (scope, query.component, item.code, item.day.isoformat(), item.ratio_pct))
            connection.run("INSERT INTO company_source_page VALUES(?,?,?,?,?,?,?)", (scope, query.component, page.page, page.pages, page.total, page.row_count, CanonicalDigest(page)))

    def ratios(self, scope: str, component: str) -> tuple[CompanyRatio, ...]:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            return tuple(CompanyRatio(row[0], date.fromisoformat(row[1]), row[2]) for row in connection.run(
                "SELECT code,day,ratio FROM company_ratio_observation WHERE scope=? AND component=? ORDER BY code,day", (scope, component),
            ))

    def task_done(self, scope: str, family: str, task: str) -> bool:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            return connection.run("SELECT 1 FROM company_source_task WHERE scope=? AND family=? AND task=?", (scope, family, task)).fetchone() is not None

    def watermark(self, scope: str) -> str:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            pages = tuple(tuple(row) for row in connection.run("SELECT component,page,pages,total,raw_count,decoded_hash FROM company_source_page WHERE scope=? ORDER BY component,page", (scope,)))
            tasks = tuple(tuple(row) for row in connection.run("SELECT family,task,accepted,input_hash FROM company_source_task WHERE scope=? AND family<>'fields' ORDER BY family,task", (scope,)))
            recovered = tuple((row[0], row[1], row[2], row[3], row[4].hex()) for row in connection.run(
                "SELECT code,source,source_key,record_hash,body_hash FROM company_recovered_input WHERE scope=? ORDER BY code,source,source_key,record_hash,body_hash", (scope,),
            ))
            return CanonicalDigest((pages, tasks, recovered))

    def record_recovered(self, scope: str, item: CompanyRecoveredInput) -> None:
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            connection.run("INSERT OR IGNORE INTO company_recovered_input VALUES(?,?,?,?,?,?,0)",
                (scope, item.code, item.source, item.source_key, item.record_hash, item.body_hash))

    def pending_recovered(self, scope: str) -> tuple[CompanyRecoveredInput, ...]:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            return tuple(CompanyRecoveredInput(*row) for row in connection.run(
                "SELECT code,source,source_key,record_hash,body_hash FROM company_recovered_input WHERE scope=? AND prepared=0 ORDER BY code,source,source_key", (scope,),
            ))

    def acknowledge_recovered(self, scope: str, items: tuple[CompanyRecoveredInput, ...]) -> None:
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            connection.run_many("UPDATE company_recovered_input SET prepared=1 WHERE scope=? AND code=? AND source=? AND source_key=? AND record_hash=? AND body_hash=?",
                ((scope, item.code, item.source, item.source_key, item.record_hash, item.body_hash) for item in items))

    def complete_task(self, scope: str, family: str, task: str, accepted: int, input_hash: str) -> None:
        if type(accepted) is not int or accepted < 0 or len(input_hash) != 64:
            raise ValueError("company_sync_source_task_invalid")
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            old = connection.run("SELECT accepted,input_hash FROM company_source_task WHERE scope=? AND family=? AND task=?", (scope, family, task)).fetchone()
            if old is not None and (old[0], old[1]) != (accepted, input_hash):
                raise ValueError("company_sync_source_task_changed")
            connection.run("INSERT OR IGNORE INTO company_source_task VALUES(?,?,?,?,?)", (scope, family, task, accepted, input_hash))

    def accept_annual(self, scope: str, task: str, rows: tuple[CompanyAnnualIncome, ...], cutoff: datetime) -> None:
        if any(item.observed_at != cutoff for item in rows):
            raise ValueError("company_annual_income_observation_time_mismatch")
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            connection.run_many("INSERT INTO company_annual_income_observation VALUES(?,?,?,?,?,?,?,?,?)", (
                (scope, item.code, item.report_date.isoformat(), item.disclosed_at.isoformat(), item.updated_at.isoformat(), item.observed_at.isoformat(), item.currency, item.operating_revenue, item.record_hash) for item in rows
            ))
            connection.run("INSERT INTO company_source_task VALUES(?,?,?,?,?)", (scope, "annual_income", task, len(rows), CanonicalDigest(rows)))

    def annuals(self, scope: str) -> tuple[CompanyAnnualIncome, ...]:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            return tuple(CompanyAnnualIncome(row[0], date.fromisoformat(row[1]), datetime.fromisoformat(row[2]),
                datetime.fromisoformat(row[3]), datetime.fromisoformat(row[4]), row[5], row[6], row[7]) for row in connection.run(
                "SELECT code,report_date,disclosed_at,updated_at,observed_at,currency,revenue,record_hash FROM company_annual_income_observation WHERE scope=? ORDER BY code,report_date,disclosed_at,updated_at", (scope,),
            ))


def _query(query: StructuredCompanyQuery, cutoff: datetime) -> None:
    today = cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()
    if (query.component not in {"pledge", "unlock"} or query.start < today - timedelta(days=365)
        or query.component == "pledge" and query.end > today
        or query.component == "unlock" and (query.start < today or query.end > today + timedelta(days=180))):
        raise ValueError("company_ratio_source_query_invalid")


__all__ = ["CompanyRecoveredInput", "SQLiteCompanySyncInputStore"]
