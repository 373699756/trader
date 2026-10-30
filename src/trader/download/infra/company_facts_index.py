"""Sole writer of the compact relational company publication."""

from __future__ import annotations

import hashlib
import json
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
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)

_DEFAULT_EVENT_POLICY = CompanyEventPolicy()


@dataclass(frozen=True, slots=True)
class CompanyPublicationIdentity:
    content_hash: bytes
    extraction_hash: bytes
    published_at: datetime


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


_CONTENT_QUERIES = (
    "SELECT * FROM company_facts ORDER BY code",
    "SELECT code,event_key,risk_type,subject_type,level,case_number,first_disclosed_at,last_disclosed_at,effective_at,status,resolved_at,expires_at FROM company_risk_event ORDER BY code,event_key",
    "SELECT e.code,e.event_key,p.start_date,p.end_date,p.planned_pct,p.completion_disclosed_at FROM company_reduction_plan p JOIN company_risk_event e ON e.id=p.risk_id ORDER BY e.code,e.event_key",
    "SELECT code,event_key,event_type,subject_type,status,first_disclosed_at,last_disclosed_at,cancelled_at,level,ineligible_reason,actual_pct,repurchase_purpose,contract_amount,annual_revenue,revenue_report_date,revenue_disclosed_at,contract_revenue_pct,binding_confirmed,amount_basis FROM company_positive_event ORDER BY code,event_key",
    "SELECT e.code,e.event_key,m.level,m.qualified_at,m.measured_pct,r.source,r.source_key FROM company_positive_milestone m JOIN company_positive_event e ON e.id=m.positive_id JOIN company_evidence_ref r ON r.id=m.evidence_id ORDER BY e.code,e.event_key,m.level",
    "SELECT * FROM company_unlock ORDER BY code,unlock_date",
    "SELECT v.code,v.scope,v.reason,r.source,r.source_key,v.detected_at FROM company_review v LEFT JOIN company_evidence_ref r ON r.id=v.evidence_id ORDER BY v.code,v.scope,v.reason,r.source,r.source_key",
    "SELECT e.code,e.event_key,l.position,r.source,r.source_key FROM company_risk_evidence l JOIN company_risk_event e ON e.id=l.risk_id JOIN company_evidence_ref r ON r.id=l.evidence_id ORDER BY e.code,e.event_key,l.position",
    "SELECT e.code,e.event_key,l.position,r.source,r.source_key FROM company_positive_evidence l JOIN company_positive_event e ON e.id=l.positive_id JOIN company_evidence_ref r ON r.id=l.evidence_id ORDER BY e.code,e.event_key,l.position",
    "SELECT code,reason,effective_at,source,source_key,evidence_hash FROM company_exclusion ORDER BY code,reason,source,source_key",
)


def _write_identity(connection: SqliteConnection, extraction_hash: bytes, at: datetime) -> None:
    digest = hashlib.sha256(extraction_hash)
    for position, query in enumerate(_CONTENT_QUERIES):
        digest.update(position.to_bytes(2, "big"))
        for row in connection.run(query):
            payload = json.dumps(
                [(["bytes", value.hex()] if isinstance(value, bytes) else value) for value in row],
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    connection.run(
        "INSERT OR REPLACE INTO company_publication VALUES(1,?,?,?,?)",
        (
            COMPANY_FACTS_VERSION,
            extraction_hash,
            digest.digest(),
            EncodeCompanyTime(at),
        ),
    )


class SQLiteCompanyFactsIndex:
    def __init__(
        self,
        path: Path,
        *,
        event_policy: CompanyEventPolicy = _DEFAULT_EVENT_POLICY,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.path = path
        self.extraction_hash = CompanyExtractionHash(event_policy)
        self._cancel_requested = cancel_requested

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteSession(
            self.path,
            SqliteConnectionOptions(journal_mode="WAL", foreign_keys=True),
            cancel_requested=self._cancel_requested,
        ) as connection:
            EnsureSqliteSchema(connection, COMPANY_FACTS_SCHEMA, allow_create=True)

    def publish(
        self,
        facts: Sequence[CompanyFacts],
        sources: Mapping[str, str],
        parent: CompanyPublicationIdentity | None,
        at: datetime,
    ) -> None:
        with (
            SqliteSession(
                self.path,
                SqliteConnectionOptions(foreign_keys=True, timeout_seconds=8),
                cancel_requested=self._cancel_requested,
            ) as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            if ReadCompanyPublicationIdentity(connection) != parent:
                raise RuntimeError("company_publication_parent_changed")
            for item in facts:
                if self._cancel_requested is not None and self._cancel_requested():
                    raise InterruptedError("company_sync_cancelled")
                WriteCompanyFactRows(connection, item, sources)
            connection.run(
                "DELETE FROM company_evidence_ref WHERE id NOT IN (SELECT evidence_id FROM company_risk_evidence "
                "UNION SELECT evidence_id FROM company_positive_evidence UNION SELECT evidence_id FROM company_positive_milestone "
                "UNION SELECT evidence_id FROM company_review WHERE evidence_id IS NOT NULL)"
            )
            _write_identity(connection, self.extraction_hash, at)

    def read_exclusions(self) -> tuple[CompanyExclusion, ...]:
        if not self.path.exists():
            return ()
        with (
            SqliteSession(self.path, SqliteConnectionOptions(read_only=True)) as connection,
            SqliteTransaction(connection),
        ):
            ReadCompanyPublicationIdentity(connection)
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
                SqliteConnectionOptions(foreign_keys=True, timeout_seconds=8),
                cancel_requested=self._cancel_requested,
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
                )
            return inserted


__all__ = ["CompanyPublicationIdentity", "ReadCompanyPublicationIdentity", "SQLiteCompanyFactsIndex"]
