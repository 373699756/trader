"""Durable work indexes for bounded company batches; facts and checkpoints keep their owners."""

from __future__ import annotations

from collections.abc import Sequence
import re

from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview, CompanyDocumentOutsideSubjectScope
from trader.download.domain.company_evidence import (
    Disclosure,
    DisclosurePartition,
    EvidenceMarket,
    Market,
)
import hashlib
from pathlib import Path
from trader.infra.sqlite_connection import SqliteConnection

def _work_policy() -> str:
    extraction = Path(__file__).resolve().parents[1] / "domain/company_event_extraction.py"
    return hashlib.sha256(Path(__file__).read_bytes() + extraction.read_bytes()).hexdigest()


def PrepareCompanyPipelineQueries(connection: SqliteConnection) -> None:
    connection.run(
        "INSERT OR IGNORE INTO disclosure_query_contracts SELECT market,start_date,end_date,'',30,'all_announcements' FROM partitions"
    )
    connection.run(
        "INSERT OR IGNORE INTO disclosure_query_contracts SELECT market,start_date,end_date,scope,30,'all_announcements' FROM disclosure_queries"
    )
    work_policy = _work_policy()
    saved = connection.run("SELECT work_policy_id FROM company_work_policy WHERE singleton=1").fetchone()
    if saved is not None and saved[0] == work_policy:
        return
    connection.run("DELETE FROM company_document_requirements")
    connection.run("DELETE FROM company_document_exclusions")
    rows = connection.run("SELECT id,title FROM disclosures WHERE kind='announcement'")
    while batch := rows.fetchmany(512):
        connection.run_many(
            "INSERT INTO company_document_requirements VALUES(?)",
            ((row[0],) for row in batch if CompanyDocumentNeedsReview(row[1])),
        )
        connection.run_many(
            "INSERT INTO company_document_exclusions VALUES(?,'subject_outside_scope')",
            ((row[0],) for row in batch if CompanyDocumentOutsideSubjectScope(row[1])),
        )
    connection.run("INSERT OR IGNORE INTO company_dirty_codes SELECT code FROM company_population")
    connection.run("INSERT OR REPLACE INTO company_work_policy VALUES(1,?)", (work_policy,))


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
        "AND NOT EXISTS(SELECT 1 FROM company_document_exclusions e WHERE e.disclosure_id=disclosures.id) "
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
        "INSERT OR IGNORE INTO company_document_exclusions SELECT id,'subject_outside_scope' "
        "FROM disclosures WHERE source=? AND source_key=?",
        ((item.source, item.identity) for item in rows
         if item.kind == 'announcement' and CompanyDocumentOutsideSubjectScope(item.title)),
    )
    connection.run_many(
        "DELETE FROM company_document_exclusions WHERE disclosure_id IN "
        "(SELECT id FROM disclosures WHERE source=? AND source_key=?)",
        ((item.source, item.identity) for item in rows
         if item.kind != 'announcement' or not CompanyDocumentOutsideSubjectScope(item.title)),
    )
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
            if item.kind != "announcement" or (
                not CompanyDocumentNeedsReview(item.title) and re.search(r"20[0-9]{2}年(?:年度|第三季度|三季度|三季)报告", item.title) is None
            )
        ),
    )


def ReplaceCompanyMaterialPopulation(connection: SqliteConnection, codes: tuple[str, ...]) -> None:
    if tuple(row[0] for row in connection.run("SELECT code FROM company_population ORDER BY code")) == codes:
        return
    connection.run("DELETE FROM company_population")
    connection.run_many("INSERT OR IGNORE INTO company_population VALUES(?)", ((code,) for code in codes))
    MarkCompanyDirty(connection, codes)


__all__ = [
    "PrepareCompanyPipelineQueries",
    "CheckDisclosureQueryContract",
    "MarkCompanyDirty",
    "ReleaseCompletedCompanyQueryRows",
    "ReleaseUnusedCompanyDisclosures",
    "MarkCompanyMarketDirty",
    "IndexCompanyDocument",
    "ReplaceCompanyMaterialPopulation",
]
