"""Thread-owned SQLite connections and explicit, bounded transaction lifetimes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from types import TracebackType
from typing import Literal, TypeAlias, cast

SqliteParameter: TypeAlias = str | int | float | bytes | None
SqliteParameters: TypeAlias = Sequence[object] | Mapping[str, object]


class SqliteResult:
    """A bounded cursor view; driver handles never leave this module."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> sqlite3.Row | None:
        return cast(sqlite3.Row | None, self._cursor.fetchone())

    def require_one(self) -> sqlite3.Row:
        row = self.fetchone()
        if row is None:
            raise sqlite3.DatabaseError("sqlite_required_row_missing")
        return row

    def fetchall(self) -> list[sqlite3.Row]:
        return self._cursor.fetchall()

    def fetchmany(self, size: int) -> list[sqlite3.Row]:
        if size < 1:
            raise ValueError("sqlite_batch_size_invalid")
        return self._cursor.fetchmany(size)

    def __iter__(self) -> Iterator[sqlite3.Row]:
        while (row := self.fetchone()) is not None:
            yield row

    def close(self) -> None:
        self._cursor.close()


class SqliteConnection:
    """Project SQLite session owning the driver, transaction and statement APIs."""

    def __init__(self, driver: sqlite3.Connection) -> None:
        self._driver = driver
        driver.row_factory = sqlite3.Row

    def run(self, sql: str, parameters: SqliteParameters = ()) -> SqliteResult:
        return SqliteResult(self._driver.execute(sql, _parameters(parameters)))

    def run_many(self, sql: str, parameters: Iterable[SqliteParameters]) -> SqliteResult:
        return SqliteResult(self._driver.executemany(sql, (_parameters(row) for row in parameters)))

    def apply_script(self, sql: str) -> None:
        """Existing schema scripts only; atomic creation uses EnsureSqliteSchema."""
        self._driver.executescript(sql)

    @property
    def in_transaction(self) -> bool:
        return self._driver.in_transaction

    @property
    def total_changes(self) -> int:
        return self._driver.total_changes

    @property
    def isolation_level(self) -> str | None:
        return self._driver.isolation_level

    def observe_statements(self, callback: Callable[[str], None] | None) -> None:
        self._driver.set_trace_callback(callback)

    def observe_progress(self, callback: Callable[[], int] | None, steps: int) -> None:
        self._driver.set_progress_handler(callback, steps)

    def commit(self) -> None:
        self._driver.commit()

    def rollback(self) -> None:
        self._driver.rollback()

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> SqliteConnection:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._driver.__exit__(exc_type, exc, traceback)


def _parameters(values: SqliteParameters) -> tuple[SqliteParameter, ...] | dict[str, SqliteParameter]:
    if isinstance(values, Mapping):
        return {key: _parameter(value) for key, value in values.items()}
    return tuple(_parameter(value) for value in values)


def _parameter(value: object) -> SqliteParameter:
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    raise ValueError("sqlite_parameter_type_invalid")


@dataclass(frozen=True)
class SqliteConnectionOptions:
    read_only: bool = False
    timeout_seconds: float = 5.0
    journal_mode: Literal["WAL", "DELETE"] | None = None
    foreign_keys: bool = False
    immutable: bool = False
    query_only: bool | None = None
    check_same_thread: bool = True
    isolation_level: Literal["DEFERRED", "IMMEDIATE", "EXCLUSIVE"] | None = "DEFERRED"
    page_size: int | None = None

    def __post_init__(self) -> None:
        if (
            not isfinite(self.timeout_seconds)
            or self.timeout_seconds < 0
            or self.read_only
            and self.journal_mode is not None
            or self.immutable
            and not self.read_only
            or self.page_size is not None
            and (self.read_only or self.page_size not in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536))
        ):
            raise ValueError("invalid_sqlite_connection_options")


_DEFAULT_OPTIONS = SqliteConnectionOptions()


def OpenSqliteConnection(
    path: Path | Literal[":memory:"],
    options: SqliteConnectionOptions = _DEFAULT_OPTIONS,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> SqliteConnection:
    """Caller closes the handle; immutable is only for offline, unchanging copies."""
    connection = sqlite3.connect(
        _connection_target(path, options),
        uri=options.read_only,
        timeout=options.timeout_seconds,
        check_same_thread=options.check_same_thread,
        isolation_level=options.isolation_level,
    )
    try:
        query_only = options.query_only if options.query_only is not None else options.read_only
        if query_only:
            connection.execute("PRAGMA query_only=ON")
        if not options.read_only:
            if options.page_size is not None:
                connection.execute(f"PRAGMA page_size={options.page_size}")
            if options.journal_mode is not None:
                actual = connection.execute(f"PRAGMA journal_mode={options.journal_mode}").fetchone()[0]
                if actual.upper() != options.journal_mode:
                    raise sqlite3.OperationalError("sqlite_journal_mode_unavailable")
            connection.execute("PRAGMA synchronous=FULL")
        if options.foreign_keys:
            connection.execute("PRAGMA foreign_keys=ON")
        if cancel_requested is not None:
            connection.set_progress_handler(lambda: int(cancel_requested()), 10_000)
        return SqliteConnection(connection)
    except BaseException:
        connection.close()
        raise


def _connection_target(path: Path | Literal[":memory:"], options: SqliteConnectionOptions) -> Path | str:
    if path == ":memory:":
        if options.read_only:
            raise ValueError("sqlite_memory_database_cannot_be_read_only")
        return path
    elif options.read_only:
        return f"{path.resolve().as_uri()}?mode=ro" + ("&immutable=1" if options.immutable else "")
    else:
        return path


@contextmanager
def SqliteTransaction(connection: SqliteConnection, *, immediate: bool = False) -> Iterator[SqliteConnection]:
    """Commit a complete unit or roll it back, including cancellation; no nested transactions."""
    if connection.in_transaction:
        raise ValueError("sqlite_nested_transaction")
    connection.run("BEGIN IMMEDIATE" if immediate else "BEGIN")
    with connection:
        yield connection


@dataclass(frozen=True)
class SqliteSchema:
    """Adapter-owned DDL statements; infrastructure owns their atomic installation."""

    version: int
    statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.version) is not int or not 1 <= self.version <= 2_147_483_647:
            raise ValueError("sqlite_schema_version_invalid")
        if not self.statements or any(
            not statement.strip().upper().startswith("CREATE ") for statement in self.statements
        ):
            raise ValueError("sqlite_schema_requires_create_statements")


def EnsureSqliteSchema(connection: SqliteConnection, schema: SqliteSchema, *, allow_create: bool = False) -> None:
    """Validate a format or atomically initialize an empty database; never migrate one."""
    version = connection.run("PRAGMA user_version").require_one()[0]
    if version == schema.version:
        return
    if version != 0 or not allow_create:
        raise sqlite3.DatabaseError("sqlite_schema_unsupported")
    with SqliteTransaction(connection, immediate=True):
        # A competing initializer may have committed while this connection waited.
        version = connection.run("PRAGMA user_version").require_one()[0]
        if version == schema.version:
            return
        if (
            version != 0
            or connection.run("SELECT 1 FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT 1").fetchone()
            is not None
        ):
            raise sqlite3.DatabaseError("sqlite_schema_unsupported")
        for statement in schema.statements:
            connection.run(statement)
        connection.run(f"PRAGMA user_version={schema.version}")


def CheckSqliteIntegrity(connection: SqliteConnection) -> None:
    """Check SQLite structural integrity without changing the database."""
    if [tuple(row) for row in connection.run("PRAGMA quick_check")] != [("ok",)]:
        raise sqlite3.DatabaseError("sqlite_integrity_failed")


__all__ = [
    "CheckSqliteIntegrity",
    "EnsureSqliteSchema",
    "OpenSqliteConnection",
    "SqliteConnection",
    "SqliteConnectionOptions",
    "SqliteParameter",
    "SqliteParameters",
    "SqliteResult",
    "SqliteSchema",
    "SqliteTransaction",
]
