"""Pin collection inputs across a pure formal commit and explicit acknowledgement."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from trader.download.application.company_publication import CompanyPublicationConflict, CompanyPublicationSession
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_evidence import CompanyListing, CompanyPublicationProgress, CompanyStockProgress
from trader.download.infra.company_contract_link_rules import ApplyCompanyContractLinkRefreshes
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity, SQLiteCompanyFactsIndex
from trader.download.infra.company_field_attempts import WriteCompanyFieldAttempts
from trader.download.infra.company_field_recovery import CompleteCompanyFieldRepairs, RegisterCompanyMaterialDependencies
from trader.download.infra.company_material_cleanup import ReleasePublishedCompanyMaterial
from trader.download.infra.company_pipeline_queries import MarkCompanyDirty
from trader.download.infra.company_publication_candidate import CompanyPublicationCandidate
from trader.download.infra.company_publication_preparation import PrepareCompanyPublicationCandidate
from trader.download.infra.company_supersession import PublishCompanySupersessions
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy


class _SQLiteCompanyPublication:
    def __init__(
        self,
        candidate: CompanyPublicationCandidate,
        source: SqliteConnection,
        source_version: int,
        connect: Callable[[], AbstractContextManager[SqliteConnection]],
        facts: SQLiteCompanyFactsIndex,
        sqlite_policy: SqliteIoPolicy,
        progress: Callable[[CompanyStockProgress | CompanyPublicationProgress], None] | None,
    ) -> None:
        self._candidate, self._source, self._source_version = candidate, source, source_version
        self._connect, self._facts, self._sqlite_policy, self._progress = connect, facts, sqlite_policy, progress
        self._scope = ExitStack()
        self._writer: SqliteConnection | None = None
        self._committed = False
        self._started = False
        self._confirmed = False
        self._closed = False
        self._publication_id = uuid4().hex
        self._changed_codes: tuple[str, ...] = ()
        self._committed_identity = candidate.parent

    @property
    def codes(self) -> tuple[str, ...]:
        return self._candidate.codes

    def commit(self) -> tuple[str, ...]:
        if self._started or self._closed:
            raise RuntimeError("company_publication_already_committed")
        self._started = True
        candidate = self._candidate
        if self._progress is not None:
            self._progress(CompanyPublicationProgress("started", self.codes, publication_id=self._publication_id))
        self._writer = self._scope.enter_context(self._connect())
        self._scope.enter_context(SqliteTransaction(self._writer, immediate=True))
        if self._source.run("PRAGMA data_version").require_one()[0] != self._source_version:
            raise CompanyPublicationConflict("company_publication_source_changed")
        try:
            committed = self._facts.publish(
                candidate.changed_facts,
                dict(candidate.sources),
                candidate.parent,
                candidate.observed_at,
                receipts=candidate.receipts,
                field_results=candidate.fields,
            )
        except RuntimeError as exc:
            if str(exc) != "company_publication_parent_changed":
                raise
            raise CompanyPublicationConflict(str(exc)) from exc
        self._changed_codes = committed.changed_codes
        self._committed_identity = committed.identity
        self._committed = True
        return self._changed_codes

    def confirm(self) -> tuple[str, ...]:
        if not self._committed or self._writer is None or self._confirmed or self._closed:
            raise RuntimeError("company_publication_not_committed")
        candidate, connection = self._candidate, self._writer
        with (SqliteSession(self._facts.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as formal,
              SqliteTransaction(formal)):
            if ReadCompanyPublicationIdentity(formal) != self._committed_identity:
                raise RuntimeError("company_publication_confirmation_changed")
            WriteCompanyFieldAttempts(connection, candidate.field_attempts)
            connection.run_many(
                "INSERT OR REPLACE INTO document_publications VALUES(?,?,?,?,?)",
                ((item.disclosure_id, item.code, item.record_hash, item.extraction_hash, item.published_at.isoformat())
                 for item in candidate.document_receipts),
            )
            ApplyCompanyContractLinkRefreshes(connection, candidate.contract_refreshes)
            repaired_codes = CompleteCompanyFieldRepairs(connection, formal, candidate.facts, self._facts.extraction_hash)
            RegisterCompanyMaterialDependencies(connection, formal, candidate.facts, self._facts.extraction_hash)
        connection.run_many("DELETE FROM company_dirty_codes WHERE code=?", ((code,) for code in self.codes))
        followup = tuple(sorted(set(repaired_codes) | set(
            PublishCompanySupersessions(connection, candidate.facts, self._facts.extraction_hash))))
        MarkCompanyDirty(connection, followup)
        ReleasePublishedCompanyMaterial(connection, self._facts.extraction_hash)
        self._scope.close()
        self._writer = None
        self._confirmed = True
        if self._progress is not None:
            self._progress(CompanyPublicationProgress("committed", self.codes, self._changed_codes, self._publication_id))
        return followup

    def close(self, failure: BaseException | None = None) -> None:
        self._closed = True
        if self._writer is not None:
            error = failure if failure is not None else RuntimeError("company_publication_unconfirmed")
            self._scope.__exit__(type(error), error, error.__traceback__)
            self._writer = None


@contextmanager
def OpenCompanyPublication(
    connect: Callable[[], AbstractContextManager[SqliteConnection]],
    facts: SQLiteCompanyFactsIndex,
    path: Path,
    codes: Sequence[str],
    observed_at: datetime,
    *,
    changed_only: bool,
    event_policy: CompanyEventPolicy,
    listings: tuple[CompanyListing, ...],
    batch_publication_reads: bool,
    read_document: Callable[[bytes], str],
    check_cancelled: Callable[[], None],
    cancel_requested: Callable[[], bool] | None,
    sqlite_policy: SqliteIoPolicy,
    stock_progress: Callable[[CompanyStockProgress | CompanyPublicationProgress], None] | None,
) -> Iterator[CompanyPublicationSession | None]:
    requested = tuple(sorted(set(codes)))
    if len(requested) > 12_000:
        raise ValueError("company_publication_population_exceeded")
    with connect() as connection, SqliteTransaction(connection, immediate=True):
        connection.run_many("INSERT OR IGNORE INTO company_population VALUES(?)", ((code,) for code in requested))
    with SqliteSession(
        path, SqliteConnectionOptions(read_only=True), cancel_requested=cancel_requested, sqlite_policy=sqlite_policy,
    ) as source:
        source_version = source.run("PRAGMA data_version").require_one()[0]
        with (
            SqliteSession(facts.path, SqliteConnectionOptions(read_only=True), cancel_requested=cancel_requested,
                          sqlite_policy=sqlite_policy) as formal,
            SqliteTransaction(source), SqliteTransaction(formal),
        ):
            candidate = PrepareCompanyPublicationCandidate(
                source,
                formal,
                requested,
                observed_at,
                extraction_hash=facts.extraction_hash,
                event_policy=event_policy,
                listings=listings,
                batch_publication_reads=batch_publication_reads,
                read_document=read_document,
                check_cancelled=check_cancelled,
                cancel_requested=cancel_requested,
                stock_progress=stock_progress,
                changed_only=changed_only,
            )
        if candidate is None:
            yield None
            return
        check_cancelled()
        session = _SQLiteCompanyPublication(candidate, source, source_version, connect, facts, sqlite_policy, stock_progress)
        try:
            yield session
        except BaseException as exc:
            session.close(exc)
            raise
        else:
            session.close()


__all__ = ["OpenCompanyPublication"]
