"""Evidence-link codec; transactions and publication identity belong to the caller."""

from __future__ import annotations

from collections.abc import Sequence

from trader.download.domain.company_document_links import CompanyPublishedEvidenceLink
from trader.download.domain.company_events import CompanyEvidenceLink
from trader.download.infra.company_facts_protocol import EncodeCompanyTime, RequireCompanyTime
from trader.infra.sqlite_connection import SqliteConnection

COMPANY_DOCUMENT_LINK_SCHEMA = (
    """CREATE TABLE company_document_link (
      code TEXT NOT NULL REFERENCES company_facts(code) ON DELETE CASCADE,
      source TEXT NOT NULL,evidence_id TEXT NOT NULL,record_hash TEXT NOT NULL,
      title TEXT NOT NULL,url TEXT NOT NULL,published_at INTEGER NOT NULL,
      body_checked INTEGER NOT NULL CHECK(body_checked IN (0,1)),
      PRIMARY KEY(code,source,evidence_id)
    ) WITHOUT ROWID""",
)


def WriteCompanyDocumentLinks(connection: SqliteConnection, links: Sequence[CompanyPublishedEvidenceLink]) -> None:
    identities = tuple((item.code, item.source, item.link.evidence_id) for item in links)
    if len(set(identities)) != len(identities):
        raise ValueError("company_document_link_duplicate")
    connection.run_many(
        "INSERT INTO company_document_link VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(code,source,evidence_id) DO UPDATE SET record_hash=excluded.record_hash,"
        "title=excluded.title,url=excluded.url,published_at=excluded.published_at,body_checked=excluded.body_checked",
        ((item.code, item.source, item.link.evidence_id, item.record_hash, item.link.title, item.link.url,
          EncodeCompanyTime(item.link.published_at), int(item.link.body_checked)) for item in links),
    )


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("company_document_link_text_invalid")
    return value


def ReadCompanyDocumentLinks(
    connection: SqliteConnection, codes: Sequence[str],
) -> tuple[CompanyPublishedEvidenceLink, ...]:
    requested = tuple(sorted(set(codes)))
    if len(requested) > 12_000 or any(
        len(code) != 6 or not code.isascii() or not code.isdigit() for code in requested
    ):
        raise ValueError("company_document_link_population_invalid")
    links: list[CompanyPublishedEvidenceLink] = []
    for offset in range(0, len(requested), 512):
        batch = requested[offset:offset + 512]
        for row in connection.run(
            "SELECT code,source,evidence_id,record_hash,title,url,published_at,body_checked "
            f"FROM company_document_link WHERE code IN ({','.join('?' for _ in batch)}) "
            "ORDER BY code,source,evidence_id", batch,
        ):
            if type(row[7]) is not int or row[7] not in (0, 1):
                raise ValueError("company_document_link_body_state_invalid")
            if len(links) >= 120_000:
                raise ValueError("company_document_link_population_exceeded")
            links.append(CompanyPublishedEvidenceLink(
                _text(row[0]), _text(row[1]), _text(row[3]),
                CompanyEvidenceLink(_text(row[2]), _text(row[4]), _text(row[5]), RequireCompanyTime(row[6]), bool(row[7])),
            ))
    return tuple(links)
