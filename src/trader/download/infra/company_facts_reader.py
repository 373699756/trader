"""Read the compact company publication without loading source bodies."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from trader.download.domain.company_events import CompanyEventPolicy, CompanyEvidenceLink
from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra.company_evidence_sql import SCHEMA
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import CompanyExtractionHash
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)

class SQLiteCompanyEvidenceReader:
    """Only the published, indexed projection is visible to recommendation."""

    def __init__(
        self, path: Path, *, event_policy: CompanyEventPolicy, raw_path: Path | None = None, sqlite_policy: SqliteIoPolicy
    ) -> None:
        self._sqlite_policy = sqlite_policy
        self.path = path
        self._raw_path = raw_path
        self._extraction_hash = CompanyExtractionHash(event_policy)

    def _publication_version(self, connection: SqliteConnection) -> str:
        identity = ReadCompanyPublicationIdentity(connection)
        if identity is None:
            return ""
        if identity.extraction_hash != self._extraction_hash:
            raise ValueError("company_publication_identity_mismatch")
        return identity.content_hash.hex()

    def document_links(self, code: str, evidence_ids: tuple[str, ...]) -> tuple[CompanyEvidenceLink, ...]:
        if not evidence_ids:
            return ()
        if len(evidence_ids) > 32 or len(code) != 6 or not code.isascii() or not code.isdigit():
            raise ValueError("company_document_query_invalid")
        if self._raw_path is None or not self._raw_path.exists():
            raise RuntimeError("company_evidence_links_unavailable")
        try:
            with SqliteSession(
                self._raw_path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy
            ) as connection:
                EnsureSqliteSchema(connection, SCHEMA)
                rows = connection.run(
                    "SELECT d.source_key,d.title,d.url,d.published_at,d.body_checked FROM disclosures d "
                    "JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE c.code=? AND d.kind='announcement' "
                    f"AND d.source_key IN ({','.join('?' for _ in evidence_ids)}) ORDER BY d.published_at DESC LIMIT 32",
                    (code, *evidence_ids),
                ).fetchall()
                return tuple(
                    CompanyEvidenceLink(row[0], row[1], row[2], datetime.fromisoformat(row[3]), bool(row[4]))
                    for row in rows
                )
        except (sqlite3.Error, ValueError) as exc:
            raise RuntimeError("company_evidence_links_unavailable") from exc

    def read(self, codes: Sequence[str]) -> tuple[CompanyFacts, ...]:
        snapshots: list[CompanyFacts] = []
        self.visit(codes, snapshots.append)
        return tuple(snapshots)

    def publication_version(self) -> str:
        if not self.path.exists():
            return ""
        try:
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
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
        if not self.path.exists():
            return
        captured: list[CompanyFacts] = []
        try:
            with SqliteSession(
                self.path, SqliteConnectionOptions(read_only=True), cancel_requested=cancel_requested, sqlite_policy=self._sqlite_policy
            ) as connection:
                with SqliteTransaction(connection):
                    if not self._publication_version(connection):
                        return
                    for offset in range(0, len(codes), 512):
                        if cancel_requested is not None and cancel_requested():
                            raise InterruptedError("company_preload_cancelled")
                        batch = tuple(codes[offset : offset + 512])
                        captured.extend(ReadCompanyFactRows(connection, batch))
        except sqlite3.Error as exc:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_preload_cancelled") from exc
            raise RuntimeError("company_snapshot_unreadable") from exc
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
