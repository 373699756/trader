"""Sole writer of the compact relational company publication."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import PublishSqliteFile, VerifiedSqlitePublication, VerifySqliteFile

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_exclusions import CompanyExclusion, CompanyExclusionReason, CompanyExclusionSource
from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra.company_fact_rows import WriteCompanyFactRows
from trader.download.infra.company_facts_protocol import (
    CompanyExtractionHash,
    EncodeCompanyTime,
    RequireCompanyTime,
)
from trader.download.infra.company_facts_schema import COMPANY_FACTS_SCHEMA, COMPANY_FACTS_VERSION
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    HasSqliteSchema,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)
from trader.infra.serialization.canonical_codec import CanonicalDigest

@dataclass(frozen=True, slots=True)
class CompanyPublicationIdentity:
    content_hash: bytes
    extraction_hash: bytes
    published_at: datetime


@dataclass(frozen=True)
class CompanyInputReceipt:
    source: str
    source_key: str
    code: str
    record_hash: str
    extraction_hash: bytes
    body_hash: bytes | None
    published_at: datetime
    outcome: str = "valid"
    policy_id: str = ""


def ReadCompanyPublicationIdentity(connection: SqliteConnection) -> CompanyPublicationIdentity | None:
    EnsureSqliteSchema(connection, COMPANY_FACTS_SCHEMA)
    row = connection.run(
        "SELECT schema_version,extraction_hash,content_hash,published_at FROM company_publication WHERE singleton=1"
    ).fetchone()
    if row is None:
        return None
    if (
        row[0] != COMPANY_FACTS_VERSION
        or not isinstance(row[1], bytes)
        or len(row[1]) != 32
        or not isinstance(row[2], bytes)
        or len(row[2]) != 32
    ):
        raise ValueError("company_publication_identity_invalid")
    return CompanyPublicationIdentity(row[2], row[1], RequireCompanyTime(row[3]))


def _write_identity(
    connection: SqliteConnection, extraction_hash: bytes, at: datetime, content_hash: bytes
) -> None:
    """Record a publication generation; file integrity is owned by the file checksum."""
    connection.run(
        "INSERT OR REPLACE INTO company_publication VALUES(1,?,?,?,?)",
        (
            COMPANY_FACTS_VERSION,
            extraction_hash,
            content_hash,
            EncodeCompanyTime(at),
        ),
    )


def _parent_identity(parent: CompanyPublicationIdentity | None) -> tuple[str, str, datetime] | None:
    if parent is None:
        return None
    return parent.content_hash.hex(), parent.extraction_hash.hex(), parent.published_at

class SQLiteCompanyFactsIndex:
    def __init__(
        self,
        path: Path,
        *,
        event_policy: CompanyEventPolicy,
        cancel_requested: Callable[[], bool] | None = None, sqlite_policy: SqliteIoPolicy,
    ) -> None:
        self._sqlite_policy = sqlite_policy
        self.path = path
        self.extraction_hash = CompanyExtractionHash(event_policy)
        self._cancel_requested = cancel_requested
        self._file_verification: VerifiedSqlitePublication | None = None

    def initialize(self) -> None:
        if self.path.exists():
            with SqliteSession(
                self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy,
            ) as connection:
                if HasSqliteSchema(connection, COMPANY_FACTS_SCHEMA):
                    return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteSession(
            self.path,
            SqliteConnectionOptions(journal_mode="WAL", foreign_keys=True),
            cancel_requested=self._cancel_requested, sqlite_policy=self._sqlite_policy,
        ) as connection:
            EnsureSqliteSchema(connection, COMPANY_FACTS_SCHEMA, allow_create=True)

    def publish_file(self) -> None:
        if self.path.is_file():
            PublishSqliteFile(self.path, COMPANY_FACTS_VERSION, sqlite_policy=self._sqlite_policy)

    def publish(
        self,
        facts: Sequence[CompanyFacts],
        sources: Mapping[str, str],
        parent: CompanyPublicationIdentity | None,
        at: datetime,
        *,
        receipts: Sequence[CompanyInputReceipt] = (),
    ) -> None:
        with (
            SqliteSession(
                self.path,
                SqliteConnectionOptions(foreign_keys=True),
                cancel_requested=self._cancel_requested, sqlite_policy=self._sqlite_policy,
            ) as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            if ReadCompanyPublicationIdentity(connection) != parent:
                raise RuntimeError("company_publication_parent_changed")
            before_changes = connection.total_changes
            for item in facts:
                if self._cancel_requested is not None and self._cancel_requested():
                    raise InterruptedError("company_sync_cancelled")
                WriteCompanyFactRows(connection, item, sources)
            connection.run_many(
                "INSERT INTO company_input_receipt VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source,source_key,code,record_hash,extraction_hash) "
                "DO UPDATE SET body_hash=excluded.body_hash,published_at=excluded.published_at,outcome=excluded.outcome,policy_id=excluded.policy_id "
                "WHERE outcome<>'valid' AND (outcome<>excluded.outcome OR policy_id<>excluded.policy_id)",
                ((r.source, r.source_key, r.code, r.record_hash, r.extraction_hash, r.body_hash, EncodeCompanyTime(r.published_at), r.outcome, r.policy_id) for r in receipts),
            )
            connection.run(
                "DELETE FROM company_evidence_ref WHERE id NOT IN (SELECT evidence_id FROM company_risk_evidence "
                "UNION SELECT evidence_id FROM company_positive_evidence UNION SELECT evidence_id FROM company_positive_milestone "
                "UNION SELECT evidence_id FROM company_review WHERE evidence_id IS NOT NULL)"
            )
            if (
                parent is None
                or parent.extraction_hash != self.extraction_hash
                or connection.total_changes != before_changes
            ):
                receipt_identities = tuple(
                    (
                        r.source, r.source_key, r.code, r.record_hash, r.extraction_hash.hex(),
                        r.body_hash.hex() if r.body_hash is not None else None,
                        r.published_at, r.outcome, r.policy_id,
                    )
                    for r in sorted(receipts, key=lambda r: (r.source, r.source_key, r.code, r.record_hash))
                )
                identity = bytes.fromhex(CanonicalDigest((
                    "company-publication-generation:1", _parent_identity(parent), self.extraction_hash.hex(),
                    tuple(sorted(facts, key=lambda fact: fact.code)), tuple(sorted(sources.items())),
                    receipt_identities, at,
                )))
                _write_identity(connection, self.extraction_hash, at, identity)
        self.publish_file()

    def read_exclusions(self) -> tuple[CompanyExclusion, ...]:
        if not self.path.exists():
            return ()
        with (
            SqliteSession(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection,
            SqliteTransaction(connection),
        ):
            if ReadCompanyPublicationIdentity(connection) is None:
                return ()
            self._file_verification = VerifySqliteFile(
                self.path,
                COMPANY_FACTS_VERSION,
                sqlite_policy=self._sqlite_policy,
                previous=self._file_verification,
            )
            return tuple(
                CompanyExclusion(
                    row[0],
                    CompanyExclusionReason(row[1]),
                    RequireCompanyTime(row[2]),
                    CompanyExclusionSource(row[3]),
                    row[4],
                    row[5],
                )
                for row in connection.run(
                    "SELECT code,reason,effective_at,source,source_key,evidence_hash FROM company_exclusion ORDER BY code,reason,source,source_key"
                ).fetchall()
            )

    def record_exclusions(self, facts: Sequence[CompanyExclusion]) -> int:
        if not facts:
            return 0
        self.initialize()
        with (
            SqliteSession(
                self.path,
                SqliteConnectionOptions(foreign_keys=True),
                cancel_requested=self._cancel_requested, sqlite_policy=self._sqlite_policy,
            ) as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            parent = ReadCompanyPublicationIdentity(connection)
            inserted = 0
            for fact in facts:
                if self._cancel_requested is not None and self._cancel_requested():
                    raise InterruptedError("company_exclusion_write_cancelled")
                key = (fact.code, int(fact.reason), int(fact.source), fact.source_key)
                old = connection.run(
                    "SELECT effective_at,evidence_hash FROM company_exclusion WHERE code=? AND reason=? AND source=? AND source_key=?",
                    key,
                ).fetchone()
                if old is not None:
                    if tuple(old) != (EncodeCompanyTime(fact.effective_at), fact.evidence_hash):
                        raise ValueError("company_exclusion_evidence_conflict")
                    continue
                connection.run(
                    "INSERT INTO company_exclusion VALUES(NULL,?,?,?,?,?,?)",
                    (
                        fact.code,
                        int(fact.reason),
                        EncodeCompanyTime(fact.effective_at),
                        int(fact.source),
                        fact.source_key,
                        fact.evidence_hash,
                    ),
                )
                inserted += 1
            if inserted:
                at = max(fact.effective_at for fact in facts)
                _write_identity(
                    connection,
                    parent.extraction_hash if parent else self.extraction_hash,
                    max(at, parent.published_at) if parent else at,
                    bytes.fromhex(CanonicalDigest((
                        "company-exclusions-generation:1", _parent_identity(parent),
                        tuple(sorted((
                            fact.code, int(fact.reason), int(fact.source), fact.source_key,
                            fact.effective_at.isoformat(), fact.evidence_hash.hex(),
                        ) for fact in facts)),
                    ))),
                )
        if inserted:
            self.publish_file()
        return inserted


__all__ = ["CompanyInputReceipt", "CompanyPublicationIdentity", "ReadCompanyPublicationIdentity", "SQLiteCompanyFactsIndex"]
