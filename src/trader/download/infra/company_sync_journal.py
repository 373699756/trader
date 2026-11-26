"""Durable fixed-round scope, physical request allowance and step acknowledgements."""

import base64
import hashlib
import json
from contextlib import contextmanager
from collections.abc import Iterator, Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from trader.download.application.company_round_progress import CompanyRequestProgress
from trader.download.application.company_failures import IsRetryableCompanySourceFailure
from trader.download.application.company_sync import CompanyDirectoryResponseReceipt
from trader.download.domain.company_evidence import CompanyDatabaseIncompatibleError, CompanyDownloadStore, IsTransientCompanyDocumentFailure
from trader.download.infra.company_disclosure_source import CNINFO_DIRECTORY_URL
from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.download.domain.company_sync import CompanySyncRoundIdentity
from trader.download.domain.company_sync import (
    CompanySyncFamily, CompanySyncFamilyResult, CompanySyncPhasePlan, CompanySyncPhaseState,
    CompanySyncResume, CompanySyncRoundScope, CompanySyncSourceBudgetExceeded, CompanySyncStepResult,
    CompanySyncUnresolvedDirectoryRequest,
)
from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.http_contracts import HttpAttempt, HttpRequest, HttpResponse
from trader.infra.regular_file import OpenRegularFile
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema, SqliteConnection, SqliteConnectionOptions, SqliteSchema, SqliteSession, SqliteTransaction,
)
from trader.infra.sqlite_policy import SqliteIoPolicy


_DIRECTORY_HOST = urlsplit(CNINFO_DIRECTORY_URL).hostname
_DIRECTORY_RESPONSE_LIMIT = 16 * 1024 * 1024
# The journal uses a NOT NULL integer. Zero reserves a deliberately offline
# run, so use a distinct value for a run with no request ceiling.
_UNLIMITED_REQUESTS = -1
_DIRECTORY_RECOVERY_GAP_PREFIX = "directory_response_recovered:"


def _can_retry_rejection(processed: int, failure: str | None, gaps: tuple[str, ...]) -> bool:
    return not processed and IsRetryableCompanySourceFailure(failure) and "supplier_input_rejected" in gaps


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
        self,
        path: Path,
        *,
        sqlite_policy: SqliteIoPolicy,
        publication_identity: Callable[[], str],
        io_budget: CompanySyncIoBudget | None = None,
        observe_requests: Callable[[CompanyRequestProgress], None] | None = None,
    ) -> None:
        self.path = path
        self._io_budget = io_budget
        self._sqlite_policy = sqlite_policy
        self._publication_identity = publication_identity
        self._observe_requests = observe_requests
        self._latest_request_progress: CompanyRequestProgress | None = None
        self._executing: tuple[str, CompanySyncFamily, int] | None = None

    @property
    def executing_step(self) -> tuple[str, CompanySyncFamily, int] | None:
        """Identify a newly started durable intent while it executes."""
        return self._executing

    def _report_requests(self, progress: CompanyRequestProgress | None) -> None:
        if progress is not None and self._observe_requests is not None and progress != self._latest_request_progress:
            self._latest_request_progress = progress
            self._observe_requests(progress)

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
                EnsureSqliteSchema(connection, _SCHEMA, validate=_validate_active_families, exact=True)

    def active(self) -> CompanySyncResume | None:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            row = connection.run("SELECT round_id,cutoff,max_source_calls FROM company_sync_round WHERE active=1").fetchone()
            if row is None:
                return None
            codes = tuple(item[0] for item in connection.run("SELECT code FROM company_sync_code WHERE round_id=? ORDER BY code", (row[0],)))
            plans = tuple(CompanySyncPhasePlan(_phase_family(item[0]), item[1], _strings(item[2]), bool(item[3])) for item in connection.run(
                "SELECT family,request_hash,gaps,refresh_on_input_change FROM company_sync_plan WHERE round_id=? ORDER BY position", (row[0],),
            ))
            limit = None if row[2] == _UNLIMITED_REQUESTS else row[2]
            return CompanySyncResume(row[0], CompanySyncRoundScope(codes, datetime.fromisoformat(row[1]), tuple(plan.family for plan in plans)), plans, limit, _source_calls(connection, row[0]))

    def begin(self, scope: CompanySyncRoundScope, plans: tuple[CompanySyncPhasePlan, ...], *, max_source_calls: int | None) -> str:
        if max_source_calls is not None and (type(max_source_calls) is not int or not 0 <= max_source_calls <= 2048):
            raise ValueError("company_sync_source_budget_invalid")
        round_id = CompanySyncRoundIdentity(scope, plans)
        progress = self._latest_request_progress
        with (
            self._session(self.path, sqlite_policy=self._sqlite_policy) as connection,
            SqliteTransaction(connection, immediate=True) as transaction,
        ):
            limit = _UNLIMITED_REQUESTS if max_source_calls is None else max_source_calls
            self._begin(connection, round_id, scope, plans, limit)
            if self._observe_requests is not None and (progress is None or progress.round_id != round_id or progress.limit != max_source_calls):
                progress = _read_request_progress(connection, round_id)
            transaction.commit()
            self._report_requests(progress)
        return round_id

    @staticmethod
    def _begin(
        connection: SqliteConnection,
        round_id: str,
        scope: CompanySyncRoundScope,
        plans: tuple[CompanySyncPhasePlan, ...],
        limit: int,
    ) -> None:
        current = connection.run("SELECT round_id,max_source_calls FROM company_sync_round WHERE active=1").fetchone()
        if current is not None:
            if current[0] != round_id or (current[1] != limit and limit != _UNLIMITED_REQUESTS):
                raise ValueError("company_sync_active_scope_must_resume")
            if current[1] != limit:
                connection.run("UPDATE company_sync_round SET max_source_calls=? WHERE round_id=?", (limit, round_id))
            for plan in plans:
                if plan.refresh_on_input_change:
                    connection.run("INSERT OR IGNORE INTO company_sync_local_plan VALUES(?,?,?,?)",
                        (round_id, plan.family.value, plan.request_hash, json.dumps(plan.qualification_gaps)))
                    connection.run("UPDATE company_sync_plan SET request_hash=?,gaps=? WHERE round_id=? AND family=? AND refresh_on_input_change=1",
                        (plan.request_hash, json.dumps(plan.qualification_gaps), round_id, plan.family.value))
            return
        old = connection.run("SELECT 1 FROM company_sync_round WHERE round_id=?", (round_id,)).fetchone()
        if old is not None:
            raise ValueError("company_sync_closed_scope_cannot_restart")
        connection.run("INSERT INTO company_sync_round VALUES(?,?,?,1)", (round_id, scope.cutoff.isoformat(), limit))
        connection.run_many("INSERT INTO company_sync_code VALUES(?,?)", ((round_id, code) for code in scope.codes))
        connection.run_many("INSERT INTO company_sync_plan VALUES(?,?,?,?,?,?)", (
            (round_id, plan.family.value, position, plan.request_hash, json.dumps(plan.qualification_gaps), int(plan.refresh_on_input_change)) for position, plan in enumerate(plans)
        ))
        connection.run_many("INSERT INTO company_sync_local_plan VALUES(?,?,?,?)", (
            (round_id, plan.family.value, plan.request_hash, json.dumps(plan.qualification_gaps)) for plan in plans if plan.refresh_on_input_change
        ))

    def finish_refresh(self, round_id: str) -> None:
        """Explicitly release a terminated round; unknown requests require reconciliation."""
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            plans = tuple(row[0] for row in connection.run("SELECT family FROM company_sync_plan WHERE round_id=?", (round_id,)))
            for family in plans:
                row = connection.run("SELECT complete,can_continue,failure,processed,gaps FROM company_sync_step WHERE round_id=? AND family=? ORDER BY step DESC LIMIT 1", (round_id, family)).fetchone()
                if row is None or not row[0] or row[1] or row[2] == "source_attempt_unconfirmed" or _can_retry_rejection(row[3], row[2], _strings(row[4])):
                    raise ValueError("company_sync_refresh_requires_terminal_scope")
            if connection.run("SELECT 1 FROM company_sync_request WHERE round_id=? AND observed=0 LIMIT 1", (round_id,)).fetchone() is not None:
                raise ValueError("company_sync_refresh_has_unknown_request")
            connection.run("UPDATE company_sync_round SET active=0 WHERE round_id=? AND active=1", (round_id,))

    def can_refresh(self, round_id: str) -> bool:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            for row in connection.run("SELECT family FROM company_sync_plan WHERE round_id=?", (round_id,)):
                step = connection.run("SELECT complete,can_continue,failure,processed,gaps FROM company_sync_step WHERE round_id=? AND family=? ORDER BY step DESC LIMIT 1", (round_id, row[0])).fetchone()
                if step is None or not step[0] or step[1] or step[2] == "source_attempt_unconfirmed" or _can_retry_rejection(step[3], step[2], _strings(step[4])):
                    return False
            return connection.run("SELECT 1 FROM company_sync_request WHERE round_id=? AND observed=0 LIMIT 1", (round_id,)).fetchone() is None

    def defer_unconfirmed_directory(self, round_id: str, next_cutoff: datetime) -> bool:
        """Leave an unrecoverable directory attempt charged to its terminal round."""
        if next_cutoff.utcoffset() is None:
            raise ValueError("company_round_cutoff_timezone_required")
        if self.directory_response(round_id) is not None:
            return False
        zone = ZoneInfo("Asia/Shanghai")
        with self._session(self.path, sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
            row = connection.run("SELECT cutoff FROM company_sync_round WHERE round_id=? AND active=1", (round_id,)).fetchone()
            if row is None or datetime.fromisoformat(row[0]).astimezone(zone).date() >= next_cutoff.astimezone(zone).date():
                return False
            resume_needed = connection.run("""
                SELECT 1 FROM company_sync_plan p LEFT JOIN company_sync_step s
                ON s.round_id=p.round_id AND s.family=p.family AND s.step=(
                    SELECT MAX(last.step) FROM company_sync_step last WHERE last.round_id=p.round_id AND last.family=p.family
                ) WHERE p.round_id=? AND (s.step IS NULL OR s.complete=0 OR s.can_continue=1) LIMIT 1
                """, (round_id,)).fetchone()
            if resume_needed is not None or connection.run("SELECT 1 FROM company_sync_step WHERE round_id=? AND complete=0 LIMIT 1",
                (round_id,)).fetchone() is not None:
                return False
            requests = tuple(connection.run("""
                SELECT r.family,r.host,r.method,r.failure,s.complete,s.can_continue,s.source_required,s.failure,s.step=(
                    SELECT MAX(last.step) FROM company_sync_step last WHERE last.round_id=r.round_id AND last.family=r.family
                ) FROM company_sync_request r JOIN company_sync_step s USING(round_id,family,step)
                WHERE r.round_id=? AND r.observed=0 ORDER BY r.attempt LIMIT 2
                """, (round_id,)))
            if len(requests) != 1 or tuple(requests[0]) != (
                CompanySyncFamily.ANNOUNCEMENTS.value, _DIRECTORY_HOST, "POST", "", 1, 0, 1, "source_attempt_unconfirmed", 1,
            ):
                return False
            connection.run("UPDATE company_sync_round SET active=0 WHERE round_id=? AND active=1", (round_id,))
        return True

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
            calls, step_calls, unknown = connection.run("""
                SELECT COUNT(*),COUNT(CASE WHEN step=? THEN 1 END),COUNT(CASE WHEN observed=0 THEN 1 END)
                FROM company_sync_request WHERE round_id=? AND family=?
                """, (row[0], round_id, family.value)).require_one()
            history = tuple(connection.run(
                "SELECT step,changed_codes,gaps,failure,state_hash FROM company_sync_step "
                "WHERE round_id=? AND family=? AND complete=1 AND deferred=0 ORDER BY step",
                (round_id, family.value),
            ))
            changed_codes = tuple(sorted({code for item in history for code in _strings(item[1])}))
            refresh, plan_gaps = connection.run("SELECT refresh_on_input_change,gaps FROM company_sync_plan WHERE round_id=? AND family=?", (round_id, family.value)).require_one()
            latest_gaps = _strings(row[5])
            failure = row[7]
            retry_rejected = not unknown and _can_retry_rejection(row[1], failure, latest_gaps)
            if refresh:
                gaps = latest_gaps
            else:
                baseline = frozenset(_strings(plan_gaps))
                history_results = tuple((item[0], _strings(item[2]), item[3], item[4]) for item in history)
                confirmed = {state_hash: step for step, item_gaps, error, state_hash in history_results
                    if error is None and baseline.issuperset(item_gaps)}
                recovered_steps = {
                    int(gap[len(_DIRECTORY_RECOVERY_GAP_PREFIX):])
                    for _, item_gaps, _, _ in history_results for gap in item_gaps if gap.startswith(_DIRECTORY_RECOVERY_GAP_PREFIX)
                } if family is CompanySyncFamily.ANNOUNCEMENTS else set()
                effective_history = tuple((item_gaps, error) for step, item_gaps, error, state_hash in history_results
                    if step not in recovered_steps and not (confirmed.get(state_hash, 0) > step and (
                        "announcement_document_failed" in item_gaps or "required_original_recovery_failed" in item_gaps
                        or error is not None and IsTransientCompanyDocumentFailure(error))))
                all_gaps = set(latest_gaps) | {gap for item_gaps, _ in effective_history for gap in item_gaps}
                if "source_budget_exhausted" not in latest_gaps:
                    all_gaps.discard("source_budget_exhausted")
                gaps = tuple(sorted(gap for gap in all_gaps if not gap.startswith(_DIRECTORY_RECOVERY_GAP_PREFIX)))
                failure = next((error for _, error in effective_history if error is not None), failure)
            result = CompanySyncStepResult(bool(row[1]), bool(row[2]) or retry_rejected, row[3], step_calls, changed_codes, gaps, row[6], failure, bool(row[8]))
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
            return _phase_family(rows[0][0]) if rows else None

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
            self._finish(connection, round_id, family.value, result)
        self._executing = None

    @staticmethod
    def _finish(connection: SqliteConnection, round_id: str, family: str, result: CompanySyncStepResult) -> None:
        changed = connection.run("""UPDATE company_sync_step SET complete=1,processed=?,can_continue=?,accepted=?,changed_codes=?,gaps=?,pending=?,failure=?,deferred=?
            WHERE round_id=? AND family=? AND complete=0""", (int(result.processed_scope_complete), int(result.can_continue), result.accepted_inputs,
            json.dumps(result.changed_codes), json.dumps(result.gaps), result.pending_count, result.failure, int(result.deferred), round_id, family)).rowcount
        if changed != 1:
            raise ValueError("company_sync_step_acknowledgement_missing")

    def authorize_request(self, request: HttpRequest) -> None:
        executing = self._executing
        if executing is None:
            raise ValueError("company_sync_request_outside_source_step")
        round_id, family, step = executing
        progress = self._latest_request_progress
        with (
            self._session(self.path, sqlite_policy=self._sqlite_policy) as connection,
            SqliteTransaction(connection, immediate=True) as transaction,
        ):
            row = connection.run("SELECT source_required,complete,state_hash FROM company_sync_step WHERE round_id=? AND family=? AND step=?", (round_id, family.value, step)).require_one()
            if not row[0] or row[1]:
                raise ValueError("company_sync_request_outside_source_step")
            maximum = connection.run("SELECT max_source_calls FROM company_sync_round WHERE round_id=? AND active=1", (round_id,)).require_one()[0]
            used = _source_calls(connection, round_id)
            if maximum != _UNLIMITED_REQUESTS and used >= maximum:
                raise CompanySyncSourceBudgetExceeded("company_sync_source_budget_exhausted")
            request_hash = _request_hash(request.method, request.url, request.body)
            host = urlsplit(request.url).hostname or ""
            if family is CompanySyncFamily.ANNOUNCEMENTS and request.method == "POST" and host == _DIRECTORY_HOST:
                unsettled = connection.run("""
                    SELECT 1 FROM company_sync_request r JOIN company_sync_round old USING(round_id)
                    JOIN company_sync_step s USING(round_id,family,step)
                    WHERE old.active=0 AND r.family=? AND r.observed=0 AND r.host=? AND r.method='POST'
                      AND s.failure='source_attempt_unconfirmed' AND (r.request_hash=? OR s.state_hash=?) LIMIT 1
                    """, (family.value, host, request_hash, row[2])).fetchone()
                if unsettled is not None:
                    raise CompanySyncUnresolvedDirectoryRequest("previous_directory_attempt_unconfirmed")
            if self._observe_requests is not None and (
                progress is None or progress.round_id != round_id or progress.used != used
            ):
                progress = _read_request_progress(connection, round_id)
            connection.run("""INSERT INTO company_sync_request(round_id,family,step,request_hash,host,method)
                VALUES(?,?,?,?,?,?)""", (round_id, family.value, step,
                request_hash, host, request.method))
            # The journal owns the one running attempt. Project the committed
            # reservation before the I/O context checks its deadline again.
            transaction.commit()
            if progress is not None:
                self._report_requests(replace(progress, used=used + 1, unknown=progress.unknown + 1))

    def retain_directory_response(self, request: HttpRequest, response: HttpResponse) -> None:
        """Save only the complete response bound to the current reserved directory request."""
        executing = self._executing
        if (executing is None or executing[1] is not CompanySyncFamily.ANNOUNCEMENTS or request.method != "POST"
            or request.url != CNINFO_DIRECTORY_URL or request.body is None or response.status_code != 200
            or not 0 < len(response.content) <= _DIRECTORY_RESPONSE_LIMIT):
            return
        round_id, family, step = executing
        request_hash = _request_hash(request.method, request.url, request.body)
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            row = connection.run("""SELECT r.attempt,s.state_hash FROM company_sync_request r
                JOIN company_sync_step s USING(round_id,family,step)
                WHERE r.round_id=? AND r.family=? AND r.step=? AND r.request_hash=? AND r.observed=0
                    AND s.complete=0 ORDER BY r.attempt DESC LIMIT 1""",
                (round_id, family.value, step, request_hash)).require_one()
        AtomicWriteJson(self._directory_response_path, {
            "round_id": round_id, "step": step, "attempt": row[0], "request_hash": request_hash, "state_hash": row[1],
            "request_body": base64.b64encode(request.body).decode("ascii"),
            "response_content": base64.b64encode(response.content).decode("ascii"),
            "content_hash": hashlib.sha256(response.content).hexdigest(), "encoding": response.encoding,
        })

    @property
    def _directory_response_path(self) -> Path:
        return self.path.with_name(self.path.name + ".directory-response.json")

    def directory_response(self, round_id: str) -> CompanyDirectoryResponseReceipt | None:
        check = self._io_budget.check if self._io_budget is not None else None
        receipt = _read_directory_receipt(self._directory_response_path, check=check)
        if receipt is None or receipt.round_id != round_id:
            return None
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            return receipt if _directory_recovery_step(connection, receipt) is not None else None

    def confirm_directory_recovery(self, receipt: CompanyDirectoryResponseReceipt, result: CompanySyncStepResult) -> None:
        if result.source_calls or not result.can_continue or result.failure is not None:
            raise ValueError("company_directory_recovery_result_invalid")
        check = self._io_budget.check if self._io_budget is not None else None
        if _read_directory_receipt(self._directory_response_path, check=check) != receipt:
            raise ValueError("company_directory_recovery_receipt_changed")
        family = CompanySyncFamily.ANNOUNCEMENTS.value
        with (
            self._session(self.path, sqlite_policy=self._sqlite_policy) as connection,
            SqliteTransaction(connection, immediate=True) as transaction,
        ):
            recovered_step = _directory_recovery_step(connection, receipt)
            if recovered_step is None:
                raise ValueError("company_directory_recovery_intent_changed")
            was_complete, was_observed = recovered_step
            connection.run("""UPDATE company_sync_request SET observed=1,status=200
                WHERE attempt=? AND round_id=?""", (receipt.attempt, receipt.round_id))
            if was_complete:
                audit_gap = f"{_DIRECTORY_RECOVERY_GAP_PREFIX}{receipt.step}"
                recovery_hash = CanonicalDigest((receipt.request_hash, receipt.state_hash, hashlib.sha256(receipt.response_content).hexdigest()))
                connection.run("""INSERT INTO company_sync_step(round_id,family,step,state_hash,source_required)
                    VALUES(?,?,?,?,0)""", (receipt.round_id, family, receipt.step + 1, recovery_hash))
                result = replace(result, gaps=(*result.gaps, audit_gap))
            self._finish(connection, receipt.round_id, family, result)
            progress = self._after_observation(connection, receipt.round_id, newly_observed=not was_observed)
            transaction.commit()
            self._report_requests(progress)
        self._executing = None

    def observe_attempt(self, attempt: HttpAttempt) -> None:
        executing = self._executing
        if executing is None:
            raise ValueError("company_sync_attempt_outside_source_step")
        round_id, family, step = executing
        with (
            self._session(self.path, sqlite_policy=self._sqlite_policy) as connection,
            SqliteTransaction(connection, immediate=True) as transaction,
        ):
            row = connection.run("SELECT attempt,host,method FROM company_sync_request WHERE round_id=? AND family=? AND step=? AND observed=0 ORDER BY attempt DESC LIMIT 1", (round_id, family.value, step)).fetchone()
            if row is None or (row[1], row[2]) != (attempt.host, attempt.method):
                raise ValueError("company_sync_attempt_identity_mismatch")
            connection.run("UPDATE company_sync_request SET observed=1,status=?,failure=? WHERE attempt=?", (attempt.status_code, attempt.failure[:160], row[0]))
            progress = self._after_observation(connection, round_id, newly_observed=True)
            transaction.commit()
            self._report_requests(progress)

    def _after_observation(
        self, connection: SqliteConnection, round_id: str, *, newly_observed: bool,
    ) -> CompanyRequestProgress | None:
        if self._observe_requests is None:
            return None
        progress = self._latest_request_progress
        if progress is None or progress.round_id != round_id or newly_observed and not progress.unknown:
            return _read_request_progress(connection, round_id)
        return replace(progress, unknown=progress.unknown - 1) if newly_observed else progress

    def publication_identity(self) -> str:
        return self._publication_identity()


def _phase_family(value: object) -> CompanySyncFamily:
    try:
        return CompanySyncFamily(value)
    except ValueError as exc:
        raise CompanyDatabaseIncompatibleError(CompanyDownloadStore.REQUEST_JOURNAL) from exc


def _validate_active_families(connection: SqliteConnection) -> None:
    for (family,) in connection.run("""
        SELECT plan.family FROM company_sync_round round JOIN company_sync_plan plan USING(round_id)
        WHERE round.active=1
        """):
        _phase_family(family)


def _request_hash(method: str, url: str, body: bytes | None) -> str:
    return CanonicalDigest((method, url, hashlib.sha256(body).hexdigest() if body is not None else None))


def _read_directory_receipt(
    path: Path, *, check: Callable[[], None] | None,
) -> CompanyDirectoryResponseReceipt | None:
    try:
        with OpenRegularFile(path, maximum_bytes=_DIRECTORY_RESPONSE_LIMIT * 2, check=check) as stream:
            payload: object = json.loads(stream.read())
        if not isinstance(payload, dict) or payload.keys() != {
            "round_id", "step", "attempt", "request_hash", "state_hash", "request_body", "response_content", "content_hash", "encoding",
        }:
            return None
        round_id, step, attempt = payload["round_id"], payload["step"], payload["attempt"]
        request_hash, state_hash = payload["request_hash"], payload["state_hash"]
        request_body, response_content, content_hash, encoding = (
            payload["request_body"], payload["response_content"], payload["content_hash"], payload["encoding"],
        )
        if (
            not isinstance(round_id, str) or not isinstance(request_hash, str) or not isinstance(state_hash, str)
            or type(step) is not int or step <= 0 or type(attempt) is not int or attempt <= 0
            or not isinstance(request_body, str) or not isinstance(response_content, str)
            or not isinstance(content_hash, str) or not isinstance(encoding, str) or not 1 <= len(encoding) <= 64
        ):
            return None
        body = base64.b64decode(request_body, validate=True)
        content = base64.b64decode(response_content, validate=True)
        if (_request_hash("POST", CNINFO_DIRECTORY_URL, body) != request_hash
            or not 0 < len(content) <= _DIRECTORY_RESPONSE_LIMIT or hashlib.sha256(content).hexdigest() != content_hash):
            return None
        return CompanyDirectoryResponseReceipt(round_id, step, attempt, request_hash, state_hash, body, content, encoding)
    except (FileNotFoundError, ValueError):
        return None


def _directory_recovery_step(
    connection: SqliteConnection, receipt: CompanyDirectoryResponseReceipt,
) -> tuple[bool, bool] | None:
    row = connection.run("""SELECT s.complete,r.observed FROM company_sync_request r
        JOIN company_sync_step s USING(round_id,family,step) JOIN company_sync_round active USING(round_id)
        WHERE r.round_id=? AND r.family='announcements' AND r.step=? AND r.attempt=? AND r.request_hash=?
          AND s.state_hash=? AND s.source_required=1 AND active.active=1
          AND r.host=? AND r.method='POST' AND r.failure='' AND (r.observed=0 OR r.status=200)
          AND (s.complete=0 OR (s.failure='source_attempt_unconfirmed' AND s.can_continue=0))
          AND s.step=(SELECT MAX(last.step) FROM company_sync_step last WHERE last.round_id=r.round_id AND last.family=r.family)
          AND r.attempt=(SELECT MAX(last.attempt) FROM company_sync_request last
              WHERE last.round_id=r.round_id AND last.family=r.family AND last.step=r.step)
          AND NOT EXISTS(SELECT 1 FROM company_sync_request unknown WHERE unknown.round_id=r.round_id
              AND unknown.family=r.family AND unknown.observed=0 AND unknown.attempt!=r.attempt)
          AND NOT EXISTS(SELECT 1 FROM company_sync_step pending WHERE pending.round_id=r.round_id
              AND pending.complete=0 AND (pending.family!='announcements' OR pending.step!=s.step))""",
        (receipt.round_id, receipt.step, receipt.attempt, receipt.request_hash, receipt.state_hash, _DIRECTORY_HOST)).fetchone()
    return None if row is None else (bool(row[0]), bool(row[1]))


def _read_request_progress(connection: SqliteConnection, round_id: str) -> CompanyRequestProgress:
    limit, used, unknown = connection.run("""
        SELECT rounds.max_source_calls,COUNT(requests.attempt),COUNT(CASE WHEN requests.observed=0 THEN 1 END)
        FROM company_sync_round rounds LEFT JOIN company_sync_request requests USING(round_id)
        WHERE rounds.round_id=? GROUP BY rounds.round_id,rounds.max_source_calls
        """, (round_id,)).require_one()
    return CompanyRequestProgress(round_id, None if limit == _UNLIMITED_REQUESTS else limit, used, unknown)


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
