"""Read the compact company publication without loading source bodies."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import VerifiedSqlitePublication, VerifySqliteFile

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from trader.download.domain.company_document_links import CompanyDocumentLinkSnapshot
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_field_results import CompanyFieldSnapshot
from trader.download.infra.company_document_link_rows import ReadCompanyDocumentLinks
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import CompanyExtractionHash
from trader.download.infra.company_facts_schema import COMPANY_FACTS_SCHEMA
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.infra.sqlite_connection import (
    SqliteConnection,
    EnsureSqliteSchema,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)

class SQLiteCompanyEvidenceReader:
    """Only the published, indexed projection is visible to recommendation."""

    def __init__(
        self, path: Path, *, event_policy: CompanyEventPolicy, sqlite_policy: SqliteIoPolicy,
        io_budget: CompanySyncIoBudget | None = None,
    ) -> None:
        self._sqlite_policy = sqlite_policy
        self._io_budget = io_budget
        self.path = path
        self._extraction_hash = CompanyExtractionHash(event_policy)
        self._file_verification: VerifiedSqlitePublication | None = None

    @contextmanager
    def _session(self, *, cancel_requested: Callable[[], bool] | None = None) -> Iterator[SqliteConnection]:
        if self._io_budget is not None:
            with self._io_budget.session(self.path, SqliteConnectionOptions(read_only=True),
                sqlite_policy=self._sqlite_policy, cancel_requested=cancel_requested) as connection:
                yield connection
        else:
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True),
                sqlite_policy=self._sqlite_policy, cancel_requested=cancel_requested) as connection:
                yield connection

    def _publication_version(self, connection: SqliteConnection, *, check: Callable[[], None] | None = None) -> str:
        if check is not None:
            check()
        EnsureSqliteSchema(connection, COMPANY_FACTS_SCHEMA, exact=True)
        identity = ReadCompanyPublicationIdentity(connection)
        if check is not None:
            check()
        if identity is None:
            return ""
        self._file_verification = VerifySqliteFile(
            self.path,
            sqlite_policy=self._sqlite_policy,
            previous=self._file_verification,
            check=check,
        )
        if identity.extraction_hash != self._extraction_hash:
            raise ValueError("company_publication_identity_mismatch")
        return identity.content_hash.hex()

    def read_document_links(
        self, codes: Sequence[str], *, cancel_requested: Callable[[], bool] | None = None,
    ) -> CompanyDocumentLinkSnapshot:
        """Preload links from the same formal owner; never inspect collection state."""
        def check() -> None:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_document_link_read_cancelled")

        check()
        if not self.path.exists():
            return CompanyDocumentLinkSnapshot("", ())
        try:
            with self._session(cancel_requested=cancel_requested) as connection:
                with SqliteTransaction(connection):
                    version = self._publication_version(connection, check=check)
                    links = ReadCompanyDocumentLinks(connection, codes) if version else ()
                    check()
                    return CompanyDocumentLinkSnapshot(version, links)
        except sqlite3.Error as exc:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_document_link_read_cancelled") from exc
            raise RuntimeError("company_evidence_links_unavailable") from exc

    def read(self, codes: Sequence[str]) -> tuple[CompanyFacts, ...]:
        snapshots: list[CompanyFacts] = []
        self.visit(codes, snapshots.append)
        return tuple(snapshots)

    def read_fields(
        self, codes: Sequence[str], *, cancel_requested: Callable[[], bool] | None = None,
    ) -> CompanyFieldSnapshot:
        """Return only published field conclusions with their pinned fact identity."""
        def check_cancelled() -> None:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_field_read_cancelled")

        check_cancelled()
        if not self.path.exists():
            return CompanyFieldSnapshot("", ())
        try:
            with self._session(cancel_requested=cancel_requested) as connection:
                with SqliteTransaction(connection):
                    version = self._publication_version(connection, check=check_cancelled)
                    results = ReadCompanyFieldResults(connection, codes) if version else ()
                    if results:
                        marker = connection.run("SELECT rules_hash FROM company_field_proof_rule WHERE singleton=1").fetchone()
                        if marker is None or marker[0] != CompanyFieldProofRulesHash():
                            raise ValueError("company_field_rules_unreproved")
                    check_cancelled()
                    return CompanyFieldSnapshot(version, results)
        except sqlite3.Error as exc:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_field_read_cancelled") from exc
            raise RuntimeError("company_fields_unreadable") from exc

    def publication_identity(self, *, cancel_requested: Callable[[], bool] | None = None) -> str:
        """Observe the committed identity without verifying or loading the fact payload."""
        if cancel_requested is not None and cancel_requested():
            raise InterruptedError("company_observation_cancelled")
        if not self.path.exists():
            return ""
        try:
            with self._session(cancel_requested=cancel_requested) as connection:
                EnsureSqliteSchema(connection, COMPANY_FACTS_SCHEMA, exact=True)
                identity = ReadCompanyPublicationIdentity(connection)
                if cancel_requested is not None and cancel_requested():
                    raise InterruptedError("company_observation_cancelled")
                if identity is None:
                    return ""
                if identity.extraction_hash != self._extraction_hash:
                    raise ValueError("company_publication_identity_mismatch")
                return identity.content_hash.hex()
        except sqlite3.Error as exc:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_observation_cancelled") from exc
            raise RuntimeError("company_publication_unreadable") from exc

    def publication_version(self) -> str:
        if not self.path.exists():
            return ""
        try:
            with self._session() as connection:
                return self._publication_version(connection)
        except sqlite3.Error as exc:
            raise RuntimeError("company_snapshot_unreadable") from exc

    def visit(
        self,
        codes: Sequence[str],
        consume: Callable[[CompanyFacts], None],
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        def check_cancelled() -> None:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_preload_cancelled")

        check_cancelled()
        if not self.path.exists():
            return
        captured: list[CompanyFacts] = []
        try:
            with self._session(cancel_requested=cancel_requested) as connection:
                with SqliteTransaction(connection):
                    if not self._publication_version(connection, check=check_cancelled):
                        return
                    for offset in range(0, len(codes), 512):
                        check_cancelled()
                        batch = tuple(codes[offset : offset + 512])
                        captured.extend(ReadCompanyFactRows(connection, batch))
                    check_cancelled()
        except sqlite3.Error as exc:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_preload_cancelled") from exc
            raise RuntimeError("company_snapshot_unreadable") from exc
        check_cancelled()
        self._consume_publication(codes, captured, consume, cancel_requested)

    @staticmethod
    def _consume_publication(
        codes: Sequence[str],
        captured: list[CompanyFacts],
        consume: Callable[[CompanyFacts], None],
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        # Invoke consumers only after releasing the pinned SQLite snapshot.
        by_code = {row.code: row for row in captured}
        for code in codes:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_preload_cancelled")
            if code not in by_code:
                continue
            consume(by_code[code])
