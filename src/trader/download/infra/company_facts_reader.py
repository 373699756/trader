"""Read the compact company publication without loading source bodies."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from trader.download.domain.company_events import CompanyEventPolicy, CompanyEvidenceLink
from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra.company_facts_codec import (
    COMPANY_FACTS_SCHEMA,
    PUBLICATION_COLUMNS,
    CompanyExtractionIdentity,
    DecodeCompanyPublication,
)
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction

_DEFAULT_EVENT_POLICY = CompanyEventPolicy()


class SQLiteCompanyEvidenceReader:
    """Only the published, indexed projection is visible to recommendation."""

    def __init__(self, path: Path, *, event_policy: CompanyEventPolicy = _DEFAULT_EVENT_POLICY) -> None:
        self.path = path
        self._extraction_identity = CompanyExtractionIdentity(event_policy)

    def _publication_version(self, connection: SqliteConnection) -> str:
        row = connection.run(
            "SELECT version,schema_identity,extraction_identity FROM publication WHERE singleton=1"
        ).fetchone()
        if row is None:
            return ""
        if row[1] != COMPANY_FACTS_SCHEMA or row[2] != self._extraction_identity:
            raise ValueError("company_publication_identity_mismatch")
        return str(row[0])

    def document_links(self, code: str, evidence_ids: tuple[str, ...]) -> tuple[CompanyEvidenceLink, ...]:
        if not evidence_ids or not self.path.exists():
            return ()
        if len(evidence_ids) > 32 or len(code) != 6 or not code.isascii() or not code.isdigit():
            raise ValueError("company_document_query_invalid")
        try:
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True, timeout_seconds=2)) as connection:
                rows = connection.run(
                    "SELECT d.source_id,d.title,d.url,d.published_at,d.body_checked FROM disclosures d "
                    "JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE c.code=? AND d.kind='announcement' "
                    f"AND d.source_id IN ({','.join('?' for _ in evidence_ids)}) ORDER BY d.published_at DESC LIMIT 32",
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
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True, timeout_seconds=2)) as connection:
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
                self.path, SqliteConnectionOptions(read_only=True, timeout_seconds=2), cancel_requested=cancel_requested
            ) as connection:
                with SqliteTransaction(connection):
                    contract = connection.run("SELECT identity FROM contract").fetchall()
                    if [tuple(row) for row in contract] != [("company_facts",)]:
                        raise ValueError("company_snapshot_contract_mismatch")
                    if not self._publication_version(connection):
                        return
                    for offset in range(0, len(codes), 512):
                        if cancel_requested is not None and cancel_requested():
                            raise InterruptedError("company_preload_cancelled")
                        batch = tuple(codes[offset : offset + 512])
                        placeholders = ",".join("?" for _ in batch)
                        captured.extend(
                            DecodeCompanyPublication(dict(row))
                            for row in connection.run(
                                f"SELECT {','.join(PUBLICATION_COLUMNS)} FROM published WHERE code IN ({placeholders})",
                                batch,
                            ).fetchall()
                        )
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
