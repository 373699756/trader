"""Durable work indexes for bounded company batches; facts and checkpoints keep their owners."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview
from trader.download.domain.company_evidence import Disclosure, DisclosurePartition, EvidenceMarket, Market
from trader.download.infra.company_facts_protocol import COMPANY_EXTRACTION_IDENTITY
from trader.infra.sqlite_connection import SqliteConnection


def EnsureCompanyPipelineQueries(connection: SqliteConnection) -> None:
    connection.run("CREATE TABLE IF NOT EXISTS company_dirty_codes(code TEXT PRIMARY KEY) WITHOUT ROWID")
    connection.run(
        "CREATE TABLE IF NOT EXISTS company_document_requirements(disclosure_id INTEGER PRIMARY KEY REFERENCES disclosures(id) ON DELETE CASCADE)"
    )
    connection.run(
        "CREATE TABLE IF NOT EXISTS company_work_policy(singleton INTEGER PRIMARY KEY CHECK(singleton=1),work_policy_id TEXT NOT NULL)"
    )
    connection.run(
        "CREATE TABLE IF NOT EXISTS disclosure_query_contracts(market TEXT NOT NULL,start_date TEXT NOT NULL,end_date TEXT NOT NULL,scope TEXT NOT NULL,page_size INTEGER NOT NULL CHECK(page_size=30),filter_identity TEXT NOT NULL CHECK(filter_identity='all_announcements'),PRIMARY KEY(market,start_date,end_date,scope)) WITHOUT ROWID"
    )
    connection.run(
        "INSERT OR IGNORE INTO disclosure_query_contracts SELECT market,start_date,end_date,'',30,'all_announcements' FROM partitions"
    )
    connection.run(
        "INSERT OR IGNORE INTO disclosure_query_contracts SELECT market,start_date,end_date,scope,30,'all_announcements' FROM disclosure_queries"
    )
    saved = connection.run("SELECT work_policy_id FROM company_work_policy WHERE singleton=1").fetchone()
    if saved is not None and saved[0] == COMPANY_EXTRACTION_IDENTITY:
        return
    connection.run("DELETE FROM company_document_requirements")
    rows = connection.run("SELECT id,title FROM disclosures WHERE kind='announcement'")
    while batch := rows.fetchmany(512):
        connection.run_many(
            "INSERT INTO company_document_requirements VALUES(?)",
            ((row[0],) for row in batch if CompanyDocumentNeedsReview(row[1])),
        )
    connection.run("INSERT OR IGNORE INTO company_dirty_codes SELECT code FROM company_population")
    connection.run("INSERT OR REPLACE INTO company_work_policy VALUES(1,?)", (COMPANY_EXTRACTION_IDENTITY,))


def CheckDisclosureQueryContract(connection: SqliteConnection, partition: DisclosurePartition) -> None:
    key = (partition.market, str(partition.start), str(partition.end), partition.query.key if partition.query else "")
    if partition.page_size != 30 or partition.filter_identity != "all_announcements":
        raise ValueError("company_query_contract_unverified")
    row = connection.run(
        "SELECT page_size,filter_identity FROM disclosure_query_contracts WHERE market=? AND start_date=? AND end_date=? AND scope=?",
        key,
    ).fetchone()
    if row is not None and tuple(row) != (partition.page_size, partition.filter_identity):
        raise ValueError("company_query_contract_mismatch")
    connection.run(
        "INSERT OR IGNORE INTO disclosure_query_contracts VALUES(?,?,?,?,?,?)",
        (*key, partition.page_size, partition.filter_identity),
    )


def MarkCompanyDirty(connection: SqliteConnection, codes: Sequence[str]) -> None:
    connection.run_many("INSERT OR IGNORE INTO company_dirty_codes VALUES(?)", ((code,) for code in codes))


def ReleaseCompletedCompanyQueryRows(connection: SqliteConnection) -> None:
    """Completed parent coverage owns the proof; only pending ranges need page members."""
    for table in ("partition_rows", "disclosure_query_rows"):
        connection.run(
            f"DELETE FROM {table} WHERE EXISTS(SELECT 1 FROM partitions p "
            f"WHERE p.market={table}.market AND p.start_date={table}.start_date AND p.end_date={table}.end_date AND p.is_complete=1)"
        )


def ReleaseUnusedCompanyDisclosures(connection: SqliteConnection, referenced: tuple[tuple[str, str], ...]) -> None:
    """Release irrelevant directory rows, preserving formal evidence and pending page proofs."""
    connection.run(
        "CREATE TEMP TABLE retained_evidence(source TEXT,source_key TEXT,PRIMARY KEY(source,source_key)) WITHOUT ROWID"
    )
    connection.run_many("INSERT INTO retained_evidence VALUES(?,?)", referenced)
    connection.run(
        "DELETE FROM disclosures WHERE kind='announcement' AND body_retained=0 AND NOT EXISTS("
        "SELECT 1 FROM company_document_requirements r WHERE r.disclosure_id=disclosures.id) "
        "AND NOT EXISTS(SELECT 1 FROM retained_evidence r WHERE r.source=disclosures.source AND r.source_key=disclosures.source_key) "
        "AND NOT EXISTS(SELECT 1 FROM partition_rows p WHERE p.source_key=disclosures.source_key) "
        "AND NOT EXISTS(SELECT 1 FROM disclosure_query_rows q WHERE q.source_key=disclosures.source_key)"
    )


def MarkCompanyMarketDirty(connection: SqliteConnection, market: Market) -> None:
    MarkCompanyDirty(
        connection,
        tuple(
            str(row[0])
            for row in connection.run("SELECT code FROM company_population")
            if EvidenceMarket(row[0]) == market
        ),
    )


def IndexCompanyDocument(connection: SqliteConnection, rows: Sequence[Disclosure]) -> None:
    connection.run_many(
        "INSERT OR IGNORE INTO company_document_requirements SELECT id FROM disclosures WHERE source=? AND source_key=?",
        (
            (item.source, item.identity)
            for item in rows
            if item.kind == "announcement" and CompanyDocumentNeedsReview(item.title)
        ),
    )
    connection.run_many(
        "DELETE FROM company_document_requirements WHERE disclosure_id IN (SELECT id FROM disclosures WHERE source=? AND source_key=?)",
        (
            (item.source, item.identity)
            for item in rows
            if item.kind != "announcement" or not CompanyDocumentNeedsReview(item.title)
        ),
    )


def PrepareCompanyPublication(connection: SqliteConnection, codes: tuple[str, ...]) -> None:
    connection.run_many("INSERT OR IGNORE INTO company_population VALUES(?)", ((code,) for code in codes))
    MarkCompanyDirty(connection, codes)


def SelectCompanyDocuments(
    connection: SqliteConnection,
    codes: Sequence[str],
    at: datetime,
    policy: str,
    excluded: Sequence[tuple[str, str, str]],
) -> None:
    connection.run("CREATE TEMP TABLE requested_codes(code TEXT PRIMARY KEY) WITHOUT ROWID")
    connection.run_many("INSERT OR IGNORE INTO temp.requested_codes VALUES(?)", ((code,) for code in codes))
    connection.run(
        "CREATE TEMP TABLE attempted_documents(source TEXT,source_key TEXT,record_hash TEXT,PRIMARY KEY(source,source_key,record_hash)) WITHOUT ROWID"
    )
    connection.run_many("INSERT INTO temp.attempted_documents VALUES(?,?,?)", excluded)
    connection.run(
        "CREATE TEMP TABLE pending_documents AS SELECT d.id, CASE WHEN r.retry_at>? OR EXISTS(SELECT 1 FROM temp.attempted_documents a "
        "WHERE a.source=d.source AND a.source_key=d.source_key AND a.record_hash=d.record_hash) THEN 0 ELSE 1 END AS due "
        "FROM company_document_requirements q JOIN disclosures d ON d.id=q.disclosure_id "
        "LEFT JOIN document_retries r ON r.disclosure_id=d.id AND r.record_hash=d.record_hash AND r.retry_policy_id=? "
        "WHERE d.body_checked=0 AND d.published_at>=? AND d.published_at<=? "
        "AND EXISTS(SELECT 1 FROM disclosure_codes c JOIN temp.requested_codes p ON p.code=c.code WHERE c.disclosure_id=d.id)",
        (at.isoformat(), policy, (at.date() - timedelta(days=365)).isoformat(), at.isoformat()),
    )


__all__ = [
    "EnsureCompanyPipelineQueries",
    "CheckDisclosureQueryContract",
    "MarkCompanyDirty",
    "ReleaseCompletedCompanyQueryRows",
    "ReleaseUnusedCompanyDisclosures",
    "MarkCompanyMarketDirty",
    "IndexCompanyDocument",
    "PrepareCompanyPublication",
    "SelectCompanyDocuments",
]
