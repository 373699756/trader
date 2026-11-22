"""Durable fixed-round scope, physical request allowance and step acknowledgements."""

import json
import hashlib
from contextlib import contextmanager
from collections.abc import Iterator, Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.download.domain.company_sync import CompanySyncRoundIdentity
from trader.download.domain.company_sync import (
    CompanySyncFamily, CompanySyncFamilyResult, CompanySyncPhasePlan, CompanySyncPhaseState,
    CompanySyncResume, CompanySyncRoundScope, CompanySyncSourceBudgetExceeded, CompanySyncStepResult,
)
from trader.infra.http_contracts import HttpAttempt, HttpRequest
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema, SqliteConnection, SqliteConnectionOptions, SqliteSchema, SqliteSession, SqliteTransaction,
)
from trader.infra.sqlite_policy import SqliteIoPolicy


_SCHEMA = SqliteSchema((
    """CREATE TABLE company_sync_round (
      round_id TEXT PRIMARY KEY,cutoff TEXT NOT NULL,max_source_calls INTEGER NOT NULL,
      active INTEGER NOT NULL CHECK(active IN (0,1))
    ) WITHOUT ROWID""",
    "CREATE UNIQUE INDEX company_sync_one_active ON company_sync_round(active) WHERE active=1",
    """CREATE TABLE company_sync_code (
      round_id TEXT NOT NULL,code TEXT NOT NULL,PRIMARY KEY(round_id,code)
    ) WITHOUT ROWID""",
    """CREATE TABLE company_sync_plan (
      round_id TEXT NOT NULL,family TEXT NOT NULL,position INTEGER NOT NULL,request_hash TEXT NOT NULL,
      gaps TEXT NOT NULL,refresh_on_input_change INTEGER NOT NULL,PRIMARY KEY(round_id,family)
    ) WITHOUT ROWID""",
    """CREATE TABLE company_sync_local_plan (
      round_id TEXT NOT NULL,family TEXT NOT NULL,request_hash TEXT NOT NULL,gaps TEXT NOT NULL,
      PRIMARY KEY(round_id,family,request_hash)
    ) WITHOUT ROWID""",
    """CREATE TABLE company_sync_step (
      round_id TEXT NOT NULL,family TEXT NOT NULL,step INTEGER NOT NULL,state_hash TEXT NOT NULL,
      source_required INTEGER NOT NULL,complete INTEGER NOT NULL DEFAULT 0,
      processed INTEGER,can_continue INTEGER,accepted INTEGER,changed_codes TEXT,gaps TEXT,
      pending INTEGER,failure TEXT,deferred INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(round_id,family,step)
    ) WITHOUT ROWID""",
    """CREATE TABLE company_sync_request (
      attempt INTEGER PRIMARY KEY,round_id TEXT NOT NULL,family TEXT NOT NULL,step INTEGER NOT NULL,
      request_hash TEXT NOT NULL,host TEXT NOT NULL,method TEXT NOT NULL,
      observed INTEGER NOT NULL DEFAULT 0,status INTEGER,failure TEXT NOT NULL DEFAULT ''
    )""",
))


class SQLiteCompanySyncRoundJournal:
    """One scope survives process restart; observer output never refunds requests.

    A request row is committed before the transport is allowed to perform I/O.
    Its request digest binds the real URL/body without persisting those values.
    The after-attempt observer supplies diagnostics only. An unobserved or
    unacknowledged attempt remains consumed and cannot authorize its own retry.
    """

    def __init__(
        self, path: Path, *, sqlite_policy: SqliteIoPolicy,
        publication_identity: Callable[[], str], io_budget: CompanySyncIoBudget | None = None,
    ) -> None:
        self.path = path
        self._io_budget = io_budget
        self._sqlite_policy = sqlite_policy
        self._publication_identity = publication_identity
        self._executing: tuple[str, CompanySyncFamily, int] | None = None

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

    def active(self) -> CompanySyncResume | None:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            row = connection.run("SELECT round_id,cutoff,max_source_calls FROM company_sync_round WHERE active=1").fetchone()
            if row is None:
                return None
            codes = tuple(item[0] for item in connection.run("SELECT code FROM company_sync_code WHERE round_id=? ORDER BY code", (row[0],)))
            plans = tuple(CompanySyncPhasePlan(CompanySyncFamily(item[0]), item[1], _strings(item[2]), bool(item[3])) for item in connection.run(
                "SELECT family,request_hash,gaps,refresh_on_input_change FROM company_sync_plan WHERE round_id=? ORDER BY position", (row[0],),
            ))
            return CompanySyncResume(row[0], CompanySyncRoundScope(codes, datetime.fromisoformat(row[1]), tuple(plan.family for plan in plans)), plans, row[2], _source_calls(connection, row[0]))

    def begin(self, scope: CompanySyncRoundScope, plans: tuple[CompanySyncPhasePlan, ...], *, max_source_calls: int) -> str:
        if type(max_source_calls) is not int or not 0 <= max_source_calls <= 2048:
            raise ValueError("company_sync_source_budget_invalid")
        round_id = CompanySyncRoundIdentity(scope, plans)
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            active = connection.run("SELECT round_id,max_source_calls FROM company_sync_round WHERE active=1").fetchone()
            if active is not None:
                if (active[0], active[1]) != (round_id, max_source_calls):
                    raise ValueError("company_sync_active_scope_must_resume")
                for plan in plans:
                    if plan.refresh_on_input_change:
                        connection.run("INSERT OR IGNORE INTO company_sync_local_plan VALUES(?,?,?,?)",
                            (round_id, plan.family.value, plan.request_hash, json.dumps(plan.qualification_gaps)))
                        connection.run("UPDATE company_sync_plan SET request_hash=?,gaps=? WHERE round_id=? AND family=? AND refresh_on_input_change=1",
                            (plan.request_hash, json.dumps(plan.qualification_gaps), round_id, plan.family.value))
                return round_id
            old = connection.run("SELECT 1 FROM company_sync_round WHERE round_id=?", (round_id,)).fetchone()
            if old is not None:
                raise ValueError("company_sync_closed_scope_cannot_restart")
            connection.run("INSERT INTO company_sync_round VALUES(?,?,?,1)", (round_id, scope.cutoff.isoformat(), max_source_calls))
            connection.run_many("INSERT INTO company_sync_code VALUES(?,?)", ((round_id, code) for code in scope.codes))
            connection.run_many("INSERT INTO company_sync_plan VALUES(?,?,?,?,?,?)", (
                (round_id, plan.family.value, position, plan.request_hash, json.dumps(plan.qualification_gaps), int(plan.refresh_on_input_change)) for position, plan in enumerate(plans)
            ))
            connection.run_many("INSERT INTO company_sync_local_plan VALUES(?,?,?,?)", (
                (round_id, plan.family.value, plan.request_hash, json.dumps(plan.qualification_gaps)) for plan in plans if plan.refresh_on_input_change
            ))
        return round_id

    def finish_refresh(self, round_id: str) -> None:
        """Explicitly release a terminated round; unknown requests require reconciliation."""
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            plans = tuple(row[0] for row in connection.run("SELECT family FROM company_sync_plan WHERE round_id=?", (round_id,)))
            for family in plans:
                row = connection.run("SELECT complete,can_continue,failure FROM company_sync_step WHERE round_id=? AND family=? ORDER BY step DESC LIMIT 1", (round_id, family)).fetchone()
                if row is None or not row[0] or row[1] or row[2] == "source_attempt_unconfirmed":
                    raise ValueError("company_sync_refresh_requires_terminal_scope")
            if connection.run("SELECT 1 FROM company_sync_request WHERE round_id=? AND observed=0 LIMIT 1", (round_id,)).fetchone() is not None:
                raise ValueError("company_sync_refresh_has_unknown_request")
            connection.run("UPDATE company_sync_round SET active=0 WHERE round_id=? AND active=1", (round_id,))

    def can_refresh(self, round_id: str) -> bool:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            for row in connection.run("SELECT family FROM company_sync_plan WHERE round_id=?", (round_id,)):
                step = connection.run("SELECT complete,can_continue,failure FROM company_sync_step WHERE round_id=? AND family=? ORDER BY step DESC LIMIT 1", (round_id, row[0])).fetchone()
                if step is None or not step[0] or step[1] or step[2] == "source_attempt_unconfirmed":
                    return False
            return connection.run("SELECT 1 FROM company_sync_request WHERE round_id=? AND observed=0 LIMIT 1", (round_id,)).fetchone() is None

    def source_calls(self, round_id: str) -> int:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            return _source_calls(connection, round_id)

    def family(self, round_id: str, family: CompanySyncFamily) -> CompanySyncFamilyResult | None:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            row = connection.run("""SELECT step,processed,can_continue,accepted,changed_codes,gaps,pending,failure,deferred FROM company_sync_step
                WHERE round_id=? AND family=? AND complete=1 ORDER BY step DESC LIMIT 1""", (round_id, family.value)).fetchone()
            if row is None:
                return None
            accepted_row = connection.run("SELECT SUM(accepted),SUM(accepted IS NULL) FROM company_sync_step WHERE round_id=? AND family=? AND complete=1", (round_id, family.value)).require_one()
            accepted = None if accepted_row[1] else accepted_row[0]
            calls = connection.run("SELECT COUNT(*) FROM company_sync_request WHERE round_id=? AND family=?", (round_id, family.value)).require_one()[0]
            step_calls = connection.run("SELECT COUNT(*) FROM company_sync_request WHERE round_id=? AND family=? AND step=?", (round_id, family.value, row[0])).require_one()[0]
            history = tuple(connection.run("SELECT changed_codes,gaps,failure FROM company_sync_step WHERE round_id=? AND family=? AND complete=1 AND deferred=0 ORDER BY step", (round_id, family.value)))
            changed_codes = tuple(sorted({code for item in history for code in _strings(item[0])}))
            refresh = connection.run("SELECT refresh_on_input_change FROM company_sync_plan WHERE round_id=? AND family=?", (round_id, family.value)).require_one()[0]
            gaps = _strings(row[5]) if refresh else tuple(sorted({gap for item in history for gap in _strings(item[1])} | set(_strings(row[5]))))
            failure = row[7] if refresh else next((item[2] for item in history if item[2] is not None), row[7])
            result = CompanySyncStepResult(bool(row[1]), bool(row[2]), row[3], step_calls, changed_codes, gaps, row[6], failure, bool(row[8]))
            unknown = connection.run("SELECT COUNT(*) FROM company_sync_request WHERE round_id=? AND family=? AND observed=0", (round_id, family.value)).require_one()[0]
            return CompanySyncFamilyResult(family, row[0], accepted, calls, result, unknown)

    def outstanding(self, round_id: str, family: CompanySyncFamily) -> CompanySyncPhaseState | None:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            row = connection.run("SELECT state_hash,source_required FROM company_sync_step WHERE round_id=? AND family=? AND complete=0", (round_id, family.value)).fetchone()
            return None if row is None else CompanySyncPhaseState(row[0], bool(row[1]))

    def completed_state(self, round_id: str, family: CompanySyncFamily) -> CompanySyncPhaseState | None:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            row = connection.run("SELECT state_hash,source_required FROM company_sync_step WHERE round_id=? AND family=? AND complete=1 ORDER BY step DESC LIMIT 1", (round_id, family.value)).fetchone()
            return None if row is None else CompanySyncPhaseState(row[0], bool(row[1]))

    def outstanding_family(self, round_id: str) -> CompanySyncFamily | None:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            rows = connection.run("SELECT family FROM company_sync_step WHERE round_id=? AND complete=0", (round_id,)).fetchall()
            if len(rows) > 1:
                raise ValueError("company_sync_multiple_unacknowledged_steps")
            return CompanySyncFamily(rows[0][0]) if rows else None

    def start(self, round_id: str, family: CompanySyncFamily, state: CompanySyncPhaseState) -> None:
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            if connection.run("SELECT 1 FROM company_sync_step WHERE round_id=? AND complete=0", (round_id,)).fetchone() is not None:
                raise ValueError("company_sync_unacknowledged_step")
            step = connection.run("SELECT COALESCE(MAX(step),0)+1 FROM company_sync_step WHERE round_id=? AND family=?", (round_id, family.value)).require_one()[0]
            connection.run("INSERT INTO company_sync_step(round_id,family,step,state_hash,source_required) VALUES(?,?,?,?,?)", (round_id, family.value, step, state.identity, int(state.source_required)))
        self._executing = (round_id, family, step)

    def outstanding_calls(self, round_id: str, family: CompanySyncFamily) -> int:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            value: object = connection.run("SELECT COUNT(*) FROM company_sync_request r JOIN company_sync_step s ON s.round_id=r.round_id AND s.family=r.family AND s.step=r.step WHERE s.round_id=? AND s.family=? AND s.complete=0", (round_id, family.value)).require_one()[0]
            if type(value) is not int:
                raise ValueError("company_sync_journal_request_count_invalid")
            return value

    def outstanding_has_unobserved_request(self, round_id: str, family: CompanySyncFamily) -> bool:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            return connection.run("""SELECT 1 FROM company_sync_request r
                JOIN company_sync_step s ON s.round_id=r.round_id AND s.family=r.family AND s.step=r.step
                WHERE s.round_id=? AND s.family=? AND s.complete=0 AND r.observed=0 LIMIT 1""",
                (round_id, family.value)).fetchone() is not None

    def finish(self, round_id: str, family: CompanySyncFamily, result: CompanySyncStepResult) -> None:
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            changed = connection.run("""UPDATE company_sync_step SET complete=1,processed=?,can_continue=?,accepted=?,changed_codes=?,gaps=?,pending=?,failure=?,deferred=?
                WHERE round_id=? AND family=? AND complete=0""", (int(result.processed_scope_complete), int(result.can_continue), result.accepted_inputs,
                json.dumps(result.changed_codes), json.dumps(result.gaps), result.pending_count, result.failure, int(result.deferred), round_id, family.value)).rowcount
            if changed != 1:
                raise ValueError("company_sync_step_acknowledgement_missing")
        self._executing = None

    def authorize_request(self, request: HttpRequest) -> None:
        executing = self._executing
        if executing is None:
            raise ValueError("company_sync_request_outside_source_step")
        round_id, family, step = executing
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            row = connection.run("SELECT source_required,complete FROM company_sync_step WHERE round_id=? AND family=? AND step=?", (round_id, family.value, step)).require_one()
            if not row[0] or row[1]:
                raise ValueError("company_sync_request_outside_source_step")
            maximum = connection.run("SELECT max_source_calls FROM company_sync_round WHERE round_id=? AND active=1", (round_id,)).require_one()[0]
            if _source_calls(connection, round_id) >= maximum:
                raise CompanySyncSourceBudgetExceeded("company_sync_source_budget_exhausted")
            connection.run("""INSERT INTO company_sync_request(round_id,family,step,request_hash,host,method)
                VALUES(?,?,?,?,?,?)""", (round_id, family.value, step,
                CanonicalDigest((request.method, request.url, hashlib.sha256(request.body).hexdigest() if request.body is not None else None)), urlsplit(request.url).hostname or "", request.method))

    def observe_attempt(self, attempt: HttpAttempt) -> None:
        executing = self._executing
        if executing is None:
            raise ValueError("company_sync_attempt_outside_source_step")
        round_id, family, step = executing
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            row = connection.run("SELECT attempt,host,method FROM company_sync_request WHERE round_id=? AND family=? AND step=? AND observed=0 ORDER BY attempt DESC LIMIT 1", (round_id, family.value, step)).fetchone()
            if row is None or (row[1], row[2]) != (attempt.host, attempt.method):
                raise ValueError("company_sync_attempt_identity_mismatch")
            connection.run("UPDATE company_sync_request SET observed=1,status=?,failure=? WHERE attempt=?", (attempt.status_code, attempt.failure[:160], row[0]))

    def publication_identity(self) -> str:
        return self._publication_identity()


def _source_calls(connection: SqliteConnection, round_id: str) -> int:
    value: object = connection.run("SELECT COUNT(*) FROM company_sync_request WHERE round_id=?", (round_id,)).require_one()[0]
    if type(value) is not int:
        raise ValueError("company_sync_journal_request_count_invalid")
    return value


def _strings(value: str) -> tuple[str, ...]:
    parsed: object = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("company_sync_journal_strings_invalid")
    return tuple(parsed)


__all__ = ["SQLiteCompanySyncRoundJournal"]
