"""Sole writer of the compact relational company publication."""

from __future__ import annotations

from contextlib import contextmanager

from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import PublishSqliteFile, VerifiedSqlitePublication, VerifySqliteFile

from collections.abc import Iterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_field_plan import BuildCompanyFieldResults
from trader.download.domain.company_field_results import (
    CompanyFieldConclusion, CompanyFieldConclusionState, CompanyFieldName,
)
from trader.download.domain.company_exclusions import CompanyExclusion, CompanyExclusionReason, CompanyExclusionSource
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_document_links import CompanyPublishedEvidenceLink
from trader.download.infra.company_document_link_rows import ReadCompanyDocumentLinks, WriteCompanyDocumentLinks
from trader.download.domain.company_normalization import CompanyNormalizationCommit
from trader.download.domain.company_source_records import CompanyFinancialSourceObservation
from trader.download.infra.company_financial_observations import WriteCompanyFinancialObservations
from trader.download.infra.company_fact_rows import ReadCompanyFactRows, WriteCompanyFactRows
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults, WriteCompanyFieldResults
from trader.download.infra.company_facts_protocol import (
    CompanyExtractionHash, EVIDENCE_SOURCES,
    EncodeCompanyTime,
    RequireCompanyTime,
)
from trader.download.infra.company_facts_schema import COMPANY_FACTS_SCHEMA
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


@dataclass(frozen=True, slots=True)
class CompanyFactsPublication:
    changed_codes: tuple[str, ...]
    identity: CompanyPublicationIdentity


def ReadCompanyPublicationIdentity(connection: SqliteConnection) -> CompanyPublicationIdentity | None:
    EnsureSqliteSchema(connection, COMPANY_FACTS_SCHEMA)
    row = connection.run(
        "SELECT extraction_hash,content_hash,published_at FROM company_publication WHERE singleton=1"
    ).fetchone()
    if row is None:
        return None
    if (
        not isinstance(row[0], bytes)
        or len(row[0]) != 32
        or not isinstance(row[1], bytes)
        or len(row[1]) != 32
    ):
        raise ValueError("company_publication_identity_invalid")
    return CompanyPublicationIdentity(row[1], row[0], RequireCompanyTime(row[2]))


def _write_identity(
    connection: SqliteConnection, extraction_hash: bytes, at: datetime, content_hash: bytes
) -> None:
    """Record a publication generation; file integrity is owned by the file checksum."""
    connection.run(
        "INSERT OR REPLACE INTO company_publication(singleton,extraction_hash,content_hash,published_at) VALUES(1,?,?,?)",
        (extraction_hash, content_hash, EncodeCompanyTime(at)),
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
        cancel_requested: Callable[[], bool] | None = None,
        io_budget: CompanySyncIoBudget | None = None, sqlite_policy: SqliteIoPolicy,
    ) -> None:
        self._sqlite_policy = sqlite_policy
        self.path = path
        self.extraction_hash = CompanyExtractionHash(event_policy)
        self._cancel_requested = cancel_requested
        self._file_verification: VerifiedSqlitePublication | None = None
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
        if self.path.exists():
            with self._session(
                self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy,
            ) as connection:
                if HasSqliteSchema(connection, COMPANY_FACTS_SCHEMA, exact=True):
                    return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._session(
            self.path,
            SqliteConnectionOptions(journal_mode="WAL", foreign_keys=True),
            cancel_requested=self._cancel_requested, sqlite_policy=self._sqlite_policy,
        ) as connection:
            EnsureSqliteSchema(connection, COMPANY_FACTS_SCHEMA, allow_create=True, exact=True)

    def publish_file(self) -> None:
        if self.path.is_file():
            policy = self._io_budget.remaining_policy(self._sqlite_policy) if self._io_budget is not None else self._sqlite_policy
            PublishSqliteFile(self.path, sqlite_policy=policy, check=self._io_budget.check if self._io_budget is not None else None)

    def validate_existing(self) -> None:
        if self.path.exists():
            with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
                EnsureSqliteSchema(connection, COMPANY_FACTS_SCHEMA, exact=True)

    def verify_publication(self) -> None:
        if not self.path.exists():
            return
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            identity = ReadCompanyPublicationIdentity(connection)
        if identity is not None:
            policy = self._io_budget.remaining_policy(self._sqlite_policy) if self._io_budget is not None else self._sqlite_policy
            self._file_verification = VerifySqliteFile(self.path, sqlite_policy=policy, previous=self._file_verification,
                check=self._io_budget.check if self._io_budget is not None else None)

    def publish(
        self,
        facts: Sequence[CompanyFacts],
        sources: Mapping[str, str],
        parent: CompanyPublicationIdentity | None,
        at: datetime,
        *,
        receipts: Sequence[CompanyInputReceipt] = (),
        field_results: Sequence[CompanyFieldConclusion] | None = None,
        financial_observations: Sequence[CompanyFinancialSourceObservation] = (),
        normalization: CompanyNormalizationCommit | None = None,
        field_rules_hash: bytes | None = None,
        document_links: Sequence[CompanyPublishedEvidenceLink] = (),
    ) -> CompanyFactsPublication:
        if field_rules_hash is not None and (not isinstance(field_rules_hash, bytes) or len(field_rules_hash) != 32):
            raise ValueError("company_field_rules_identity_invalid")
        prepared_fields = tuple(field_results) if field_results is not None else tuple(
            CompanyFieldConclusion(
                item.code, item.event_id, cast(CompanyFieldName, item.field),
                float(item.value) if type(item.value) is int else item.value,
                cast(CompanyFieldConclusionState, item.state), item.applicable, item.reason, (),
            ) for fact in facts for item in BuildCompanyFieldResults(fact)
        )
        with (
            self._session(
                self.path,
                SqliteConnectionOptions(foreign_keys=True),
                cancel_requested=self._cancel_requested, sqlite_policy=self._sqlite_policy,
            ) as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            if ReadCompanyPublicationIdentity(connection) != parent:
                raise RuntimeError("company_publication_parent_changed")
            before_changes = connection.total_changes
            fact_codes = frozenset(fact.code for fact in facts)
            changed_codes = set(fact_codes)
            field_codes = tuple(sorted(fact_codes | {item.code for item in prepared_fields}))
            previous_fields = ReadCompanyFieldResults(connection, field_codes)
            link_codes = tuple(sorted(set(field_codes) | {item.code for item in document_links}))
            previous_links = ReadCompanyDocumentLinks(connection, link_codes)
            for item in facts:
                if self._io_budget is not None:
                    self._io_budget.check()
                if self._cancel_requested is not None and self._cancel_requested():
                    raise InterruptedError("company_sync_cancelled")
                WriteCompanyFactRows(connection, item, sources)
            for code in field_codes:
                values = tuple(sorted((item for item in prepared_fields if item.code == code),
                                      key=lambda item: (item.event_id, item.field)))
                if code in fact_codes or tuple(item for item in previous_fields if item.code == code) != values:
                    WriteCompanyFieldResults(connection, code, values)
                    changed_codes.add(code)
            if field_rules_hash is not None:
                previous_rules = connection.run("SELECT rules_hash FROM company_field_proof_rule WHERE singleton=1").fetchone()
                if previous_rules is None or previous_rules[0] != field_rules_hash:
                    all_codes = tuple(row[0] for row in connection.run("SELECT code FROM company_facts ORDER BY code"))
                    expected = {(field.code, field.event_id, field.field) for fact in ReadCompanyFactRows(connection, all_codes)
                                for field in BuildCompanyFieldResults(fact)}
                    supplied = {(field.code, field.event_id, field.field) for field in prepared_fields}
                    if supplied != expected:
                        raise ValueError("company_field_rules_full_reproof_required")
                connection.run("INSERT INTO company_field_proof_rule VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET "
                               "rules_hash=excluded.rules_hash WHERE rules_hash<>excluded.rules_hash", (field_rules_hash,))
            elif field_codes:
                connection.run("DELETE FROM company_field_proof_rule")
            WriteCompanyFinancialObservations(connection, financial_observations)
            connection.run_many(
                "INSERT INTO company_input_receipt VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source,source_key,code,record_hash,extraction_hash) "
                "DO UPDATE SET body_hash=excluded.body_hash,published_at=excluded.published_at,outcome=excluded.outcome,policy_id=excluded.policy_id "
                "WHERE outcome<>'valid' AND (outcome<>excluded.outcome OR policy_id<>excluded.policy_id)",
                ((r.source, r.source_key, r.code, r.record_hash, r.extraction_hash, r.body_hash, EncodeCompanyTime(r.published_at), r.outcome, r.policy_id) for r in receipts),
            )
            published_links = _publish_document_links(connection, previous_links, document_links,
                                                       link_codes, self.extraction_hash, at)
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
                field_identities = tuple(
                    (result.code, result.event_id, result.field, result.value, result.state, result.applicable,
                     result.reason, tuple(
                         (proof.source, proof.source_key, proof.record_hash, proof.body_hash.hex(), proof.rule_id,
                          proof.published_at, proof.start_line, proof.end_line, proof.quote, proof.unit, proof.subject.value)
                         for proof in result.evidence
                     )) for result in sorted(prepared_fields, key=lambda result: (result.code, result.event_id, result.field))
                )
                identity = bytes.fromhex(CanonicalDigest((
                    "company-publication-generation", _parent_identity(parent), self.extraction_hash.hex(),
                    tuple(sorted(facts, key=lambda fact: fact.code)), tuple(sorted(sources.items())),
                    receipt_identities, field_identities, published_links, field_rules_hash.hex() if field_rules_hash else None,
                    tuple(sorted(financial_observations, key=lambda item: item.business_key)), at,
                )))
                _write_identity(connection, self.extraction_hash, at, identity)
            committed_identity = ReadCompanyPublicationIdentity(connection)
            if committed_identity is None:
                raise RuntimeError("company_publication_identity_missing")
            if normalization is not None:
                marker = connection.run("SELECT generation FROM company_normalization_commit WHERE scope=?", (normalization.scope,)).fetchone()
                if marker is not None and marker[0] != normalization.generation:
                    raise ValueError("company_normalization_scope_generation_changed")
                connection.run(
                    "INSERT OR REPLACE INTO company_normalization_commit VALUES(?,?,?,?,?)",
                    (normalization.scope, normalization.generation, committed_identity.content_hash,
                     committed_identity.extraction_hash, EncodeCompanyTime(committed_identity.published_at)),
                )
        self.publish_file()
        return CompanyFactsPublication(tuple(sorted(changed_codes)), committed_identity)

    def read_exclusions(self) -> tuple[CompanyExclusion, ...]:
        if not self.path.exists():
            return ()
        with (
            self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection,
            SqliteTransaction(connection),
        ):
            if ReadCompanyPublicationIdentity(connection) is None:
                return ()
            self._file_verification = VerifySqliteFile(
                self.path,
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
            self._session(
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
                        "company-exclusions-generation", _parent_identity(parent),
                        tuple(sorted((
                            fact.code, int(fact.reason), int(fact.source), fact.source_key,
                            fact.effective_at.isoformat(), fact.evidence_hash.hex(),
                        ) for fact in facts)),
                    ))),
                )
        if inserted:
            self.publish_file()
        return inserted


__all__ = ["CompanyInputReceipt", "CompanyPublicationIdentity", "CompanyFactsPublication", "ReadCompanyPublicationIdentity", "SQLiteCompanyFactsIndex"]


def _published_link_reference(connection: SqliteConnection, item: CompanyPublishedEvidenceLink) -> bool:
    if item.source not in EVIDENCE_SOURCES:
        return False
    parameters = (item.code,EVIDENCE_SOURCES[item.source],item.link.evidence_id)
    return connection.run(
        "SELECT 1 FROM company_evidence_ref r WHERE r.source=? AND r.source_key=? AND ("
        "EXISTS(SELECT 1 FROM company_risk_event e JOIN company_risk_evidence l ON l.risk_id=e.id WHERE e.code=? AND l.evidence_id=r.id) OR "
        "EXISTS(SELECT 1 FROM company_positive_event e JOIN company_positive_evidence l ON l.positive_id=e.id WHERE e.code=? AND l.evidence_id=r.id) OR "
        "EXISTS(SELECT 1 FROM company_positive_event e JOIN company_positive_milestone l ON l.positive_id=e.id WHERE e.code=? AND l.evidence_id=r.id) OR "
        "EXISTS(SELECT 1 FROM company_review v WHERE v.code=? AND v.evidence_id=r.id)) "
        "UNION ALL SELECT 1 FROM company_field_evidence WHERE code=? AND source=? AND source_key=? AND record_hash=? LIMIT 1",
        (parameters[1],parameters[2],parameters[0],parameters[0],parameters[0],parameters[0],
         item.code,item.source,item.link.evidence_id,item.record_hash),
    ).fetchone() is not None


def _publish_document_links(
    connection: SqliteConnection,
    previous: tuple[CompanyPublishedEvidenceLink, ...],
    supplied: Sequence[CompanyPublishedEvidenceLink],
    codes: tuple[str, ...],
    extraction_hash: bytes,
    cutoff: datetime,
) -> tuple[CompanyPublishedEvidenceLink, ...]:
    keys = tuple((item.code,item.source,item.link.evidence_id) for item in supplied)
    if len(set(keys)) != len(keys):
        raise ValueError("company_document_link_duplicate")
    requested = set(keys)
    candidates = {(item.code,item.source,item.link.evidence_id):item for item in (*previous,*supplied)}
    retained: list[CompanyPublishedEvidenceLink] = []
    for identity,item in sorted(candidates.items()):
        referenced = _published_link_reference(connection,item)
        if not referenced:
            if identity in requested:
                raise ValueError("company_document_link_reference_missing")
            continue
        receipt = connection.run(
            "SELECT body_hash,outcome,published_at FROM company_input_receipt WHERE source=? AND source_key=? "
            "AND code=? AND record_hash=? AND extraction_hash=?",
            (item.source,item.link.evidence_id,item.code,item.record_hash,extraction_hash),
        ).fetchone()
        valid = receipt is not None and item.link.published_at <= cutoff and RequireCompanyTime(receipt[2]) >= item.link.published_at and (
            receipt[1] == "valid" and (not item.link.body_checked or receipt[0] is not None)
            or not item.link.body_checked and receipt[1] == "directory_observed")
        if not valid:
            if identity in requested:
                raise ValueError("company_document_link_receipt_missing")
            # An older retained reference remains attached to its old exact
            # receipt. Incompatible rule identities never certify a new link.
            continue
        retained.append(item)
    public_ids = tuple((item.code,item.link.evidence_id) for item in retained)
    if len(set(public_ids)) != len(public_ids):
        raise ValueError("company_document_link_source_ambiguous")
    expected = tuple(retained)
    current = ReadCompanyDocumentLinks(connection,codes)
    if current != expected:
        for code in codes:
            connection.run("DELETE FROM company_document_link WHERE code=?",(code,))
        WriteCompanyDocumentLinks(connection,expected)
    return expected
