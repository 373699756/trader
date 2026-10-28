"""Thread-owned SQLite connections and explicit, bounded transaction lifetimes."""

from __future__ import annotations

import re
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
        yield from self._cursor

    def close(self) -> None:
        self._cursor.close()


class SqliteConnection:
    """Thread-owned driver handle exposing bounded statement operations."""

    def __init__(self, driver: sqlite3.Connection) -> None:
        self._driver = driver
        driver.row_factory = sqlite3.Row

    def run(self, sql: str, parameters: SqliteParameters = ()) -> SqliteResult:
        return SqliteResult(self._driver.execute(sql, _parameters(parameters)))

    def run_many(self, sql: str, parameters: Iterable[SqliteParameters]) -> SqliteResult:
        return SqliteResult(self._driver.executemany(sql, (_parameters(row) for row in parameters)))

    @property
    def in_transaction(self) -> bool:
        return self._driver.in_transaction

    @property
    def total_changes(self) -> int:
        return self._driver.total_changes

    def observe_statements(self, callback: Callable[[str], None] | None) -> None:
        self._driver.set_trace_callback(callback)

    def observe_progress(self, callback: Callable[[], int] | None, steps: int) -> None:
        self._driver.set_progress_handler(callback, steps)

    def backup_to(self, target: SqliteConnection, *, deadline: float, monotonic: Callable[[], float]) -> None:
        """Copy a consistent snapshot, including committed WAL pages, within a caller-owned deadline."""
        if not isfinite(deadline):
            raise ValueError("sqlite_backup_deadline_invalid")

        def check_progress(status: int, remaining: int, total: int) -> None:
            if monotonic() >= deadline:
                raise TimeoutError("sqlite_backup_deadline_exceeded")

        check_progress(0, 0, 0)
        self._driver.backup(target._driver, pages=256, progress=check_progress, sleep=0.05)

    def close(self) -> None:
        """Close the handle, rolling back any unfinished transaction."""
        self._driver.close()


def _parameters(values: SqliteParameters) -> tuple[SqliteParameter, ...] | dict[str, SqliteParameter]:
    parameters = dict(values) if isinstance(values, Mapping) else tuple(values)
    for value in parameters.values() if isinstance(parameters, dict) else parameters:
        if value is not None and not isinstance(value, (str, int, float, bytes)):
            raise ValueError("sqlite_parameter_type_invalid")
    return cast(tuple[SqliteParameter, ...] | dict[str, SqliteParameter], parameters)


@dataclass(frozen=True)
class SqliteConnectionOptions:
    read_only: bool = False
    timeout_seconds: float = 5.0
    journal_mode: Literal["WAL", "DELETE", "OFF"] | None = None
    foreign_keys: bool = False
    immutable: bool = False
    query_only: bool | None = None
    check_same_thread: bool = True
    page_size: int | None = None
    cache_kib: int | None = None
    mmap_bytes: int | None = None
    temp_storage: Literal["DEFAULT", "FILE", "MEMORY"] | None = None
    durability: Literal["durable", "rebuildable"] = "durable"

    def __post_init__(self) -> None:
        if (
            self.durability not in {"durable", "rebuildable"}
            or self.journal_mode not in {None, "WAL", "DELETE", "OFF"}
            or self.temp_storage not in {None, "DEFAULT", "FILE", "MEMORY"}
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds < 0
            or self.read_only
            and self.journal_mode is not None
            or self.immutable
            and not self.read_only
            or self.page_size is not None
            and (self.read_only or self.page_size not in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536))
            or self.cache_kib is not None
            and self.cache_kib < 1
            or self.mmap_bytes is not None
            and self.mmap_bytes < 0
            or self.journal_mode == "OFF"
            and self.durability != "rebuildable"
            or self.durability == "rebuildable"
            and (self.read_only or self.journal_mode != "OFF")
        ):
            raise ValueError("invalid_sqlite_connection_options")


_DEFAULT_OPTIONS = SqliteConnectionOptions()


def _configure_database(connection: sqlite3.Connection, options: SqliteConnectionOptions) -> None:
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
        connection.execute("PRAGMA synchronous=FULL" if options.durability == "durable" else "PRAGMA synchronous=OFF")
    if options.foreign_keys:
        connection.execute("PRAGMA foreign_keys=ON")


def OpenSqliteConnection(
    path: Path | Literal[":memory:"],
    options: SqliteConnectionOptions = _DEFAULT_OPTIONS,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    statement_trace: Callable[[str], None] | None = None,
) -> SqliteConnection:
    """Caller closes the handle; immutable is only for offline, unchanging copies."""
    connection = sqlite3.connect(
        _connection_target(path, options),
        uri=options.read_only,
        timeout=options.timeout_seconds,
        check_same_thread=options.check_same_thread,
        isolation_level=None,
    )
    try:
        _configure_database(connection, options)
        if cancel_requested is not None:
            connection.set_progress_handler(lambda: int(cancel_requested()), 10_000)
        if statement_trace is not None:
            connection.set_trace_callback(statement_trace)
        if options.cache_kib is not None:
            connection.execute(f"PRAGMA cache_size=-{options.cache_kib}")
        if options.mmap_bytes is not None:
            connection.execute(f"PRAGMA mmap_size={options.mmap_bytes}")
        if options.temp_storage is not None:
            connection.execute(f"PRAGMA temp_store={options.temp_storage}")
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
def SqliteSession(
    path: Path | Literal[":memory:"],
    options: SqliteConnectionOptions = _DEFAULT_OPTIONS,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    statement_trace: Callable[[str], None] | None = None,
) -> Iterator[SqliteConnection]:
    """Own a connection lifetime; transactions are explicit and never committed on close."""
    connection = OpenSqliteConnection(path, options, cancel_requested=cancel_requested, statement_trace=statement_trace)
    try:
        yield connection
    finally:
        connection.close()


class SqliteTransaction:
    """One explicit transaction, optionally spanning methods on a thread-owned session."""

    def __init__(self, connection: SqliteConnection, *, immediate: bool = False) -> None:
        self._connection = connection
        self._immediate = immediate
        self._active = False

    def begin(self) -> None:
        if self._active or self._connection.in_transaction:
            raise ValueError("sqlite_nested_transaction")
        self._connection.run("BEGIN IMMEDIATE" if self._immediate else "BEGIN")
        self._active = True

    def commit(self) -> None:
        if not self._active:
            raise ValueError("sqlite_transaction_inactive")
        try:
            self._connection._driver.commit()
        except BaseException:
            self.rollback()
            raise
        self._active = False

    def rollback(self) -> None:
        if self._active:
            try:
                self._connection._driver.rollback()
            finally:
                self._active = False

    def __enter__(self) -> SqliteTransaction:
        self.begin()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if exc_type is not None:
            self.rollback()
        elif self._active:
            self.commit()


@dataclass(frozen=True)
class SqliteSchema:
    """Adapter-owned DDL statements; infrastructure owns their atomic installation."""

    version: int | None
    statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.version is not None and (type(self.version) is not int or not 1 <= self.version <= 2_147_483_647):
            raise ValueError("sqlite_schema_version_invalid")
        if not self.statements or any(
            not statement.strip().upper().startswith("CREATE ") for statement in self.statements
        ):
            raise ValueError("sqlite_schema_requires_create_statements")


def _schema_tokens(statement: str) -> tuple[str, ...]:
    statement = re.sub(r"\bIF\s+NOT\s+EXISTS\b", "", statement, count=1, flags=re.IGNORECASE)
    tokens = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\w+|[^\w\s]", statement.rstrip().rstrip(";"))
    return tuple(token if token.startswith(("'", '"')) else token.lower() for token in tokens)


def _validate_unversioned_schema(connection: SqliteConnection, schema: SqliteSchema) -> None:
    """Unversioned formats retain their exact adapter-owned DDL instead of acquiring a second identity."""
    actual = {
        str(row[0]): str(row[1]) for row in connection.run("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")
    }
    for statement in schema.statements:
        match = re.match(
            r"\s*CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
            statement,
            re.IGNORECASE,
        )
        if match is None or match[1] not in actual or _schema_tokens(actual[match[1]]) != _schema_tokens(statement):
            raise sqlite3.DatabaseError("sqlite_schema_unsupported")


def HasSqliteSchema(
    connection: SqliteConnection,
    schema: SqliteSchema,
    validate: Callable[[SqliteConnection], None] | None = None,
) -> bool:
    """Return false only for an uninitialized database; reject incompatible schemas."""
    # Read both facts in one SQLite statement so a concurrent initializer cannot
    # make us combine the old version with its newly committed tables.
    version, populated = connection.run(
        "SELECT user_version, EXISTS(SELECT 1 FROM sqlite_master WHERE name NOT GLOB 'sqlite_*') "
        "FROM pragma_user_version"
    ).require_one()
    if schema.version is not None and version == schema.version:
        if validate is not None:
            validate(connection)
        return True
    if version != 0:
        raise sqlite3.DatabaseError("sqlite_schema_unsupported")
    if not populated:
        return False
    if schema.version is not None:
        raise sqlite3.DatabaseError("sqlite_schema_unsupported")
    _validate_unversioned_schema(connection, schema)
    if validate is not None:
        validate(connection)
    return True


def EnsureSqliteSchema(
    connection: SqliteConnection,
    schema: SqliteSchema,
    *,
    allow_create: bool = False,
    initialize: Callable[[SqliteConnection], None] | None = None,
    validate: Callable[[SqliteConnection], None] | None = None,
) -> None:
    """Validate or initialize an empty database inside the caller's transaction when supplied."""
    if HasSqliteSchema(connection, schema, validate):
        return
    if not allow_create:
        raise sqlite3.DatabaseError("sqlite_schema_unsupported")
    if connection.in_transaction:
        _initialize_schema(connection, schema, initialize, validate)
        return
    with SqliteTransaction(connection, immediate=True):
        _initialize_schema(connection, schema, initialize, validate)


def _initialize_schema(
    connection: SqliteConnection,
    schema: SqliteSchema,
    initialize: Callable[[SqliteConnection], None] | None,
    validate: Callable[[SqliteConnection], None] | None,
) -> None:
    if HasSqliteSchema(connection, schema, validate):
        return
    for statement in schema.statements:
        connection.run(statement)
    if initialize is not None:
        initialize(connection)
    if schema.version is not None:
        connection.run(f"PRAGMA user_version={schema.version}")
    if validate is not None:
        validate(connection)


def CheckSqliteIntegrity(connection: SqliteConnection) -> None:
    """Check SQLite structural integrity without changing the database."""
    if [tuple(row) for row in connection.run("PRAGMA quick_check")] != [("ok",)]:
        raise sqlite3.DatabaseError("sqlite_integrity_failed")


__all__ = [
    "CheckSqliteIntegrity",
    "EnsureSqliteSchema",
    "HasSqliteSchema",
    "OpenSqliteConnection",
    "SqliteConnection",
    "SqliteConnectionOptions",
    "SqliteParameter",
    "SqliteParameters",
    "SqliteResult",
    "SqliteSchema",
    "SqliteSession",
    "SqliteTransaction",
]
