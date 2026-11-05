"""Release published company inputs while preserving unresolved evidence dependencies."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from trader.download.domain.company_evidence import IsTransientCompanyDocumentFailure
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_facts_protocol import DecodeCompanyLabel, EVIDENCE_SOURCES
from trader.download.infra.company_pipeline_queries import ReleaseUnusedCompanyDisclosures
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.infra.sqlite_connection import SqliteConnection


def ReleasePublishedCompanyMaterial(connection: SqliteConnection, extraction_hash: bytes) -> None:
    """Commit the release decision before physical deletion; recovery can repeat it."""
    connection.run(
        "UPDATE disclosures SET body_retained=0 WHERE body_retained=1 AND NOT EXISTS("
        "SELECT 1 FROM company_field_repairs f WHERE f.disclosure_id=disclosures.id "
        "AND f.record_hash=disclosures.record_hash AND f.state<>'complete') AND NOT EXISTS("
        "SELECT 1 FROM company_material_dependencies m WHERE m.disclosure_id=disclosures.id "
        "AND m.record_hash=disclosures.record_hash AND m.body_hash=disclosures.body_hash) "
        "AND NOT EXISTS(SELECT 1 FROM disclosure_codes c JOIN company_population u ON u.code=c.code "
        "WHERE c.disclosure_id=disclosures.id AND NOT EXISTS("
        "SELECT 1 FROM document_publications p WHERE p.disclosure_id=disclosures.id "
        "AND p.code=c.code AND p.record_hash=disclosures.record_hash AND p.extraction_hash=?))",
        (extraction_hash,),
    )


def ReleaseCompanyMaterialFiles(
    connection: SqliteConnection,
    formal: SqliteConnection,
    documents: CompanyDocumentFiles,
    raw_files: CompanyRawFiles | None,
    extraction_hash: bytes,
    cutoff: datetime | None,
) -> None:
    """Use caller-owned transactions/connections so deletion cannot race raw acceptance."""
    if raw_files is not None:
        _release_obsolete(connection, raw_files, cutoff)
        rows = connection.run(
            "SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
            "JOIN document_tasks t ON t.source=d.source AND t.source_key=d.source_key AND t.record_hash=d.record_hash "
            "WHERE d.body_retained=0 AND d.body_checked=1 AND t.stage<>'cleaned' "
            "AND NOT EXISTS(SELECT 1 FROM company_material_dependencies m WHERE m.disclosure_id=d.id "
            "AND m.record_hash=t.record_hash) GROUP BY d.id"
        ).fetchall()
        for row in rows:
            item = DecodeCompanyDisclosure(row)
            if raw_files.release(item):
                connection.run(
                    "UPDATE document_tasks SET stage='cleaned',path=NULL WHERE source=? AND source_key=? AND record_hash=?",
                    (item.source, item.identity, item.digest),
                )
        _release_terminal_failures(connection, formal, raw_files, extraction_hash)
    retained = frozenset(row[0] for row in connection.run(
        "SELECT body_hash FROM disclosures WHERE body_retained=1 AND body_hash IS NOT NULL "
        "UNION SELECT body_hash FROM company_material_dependencies"
    ))
    documents.release(retained)
    referenced = tuple(
        (DecodeCompanyLabel(EVIDENCE_SOURCES, row[0]), row[1])
        for row in formal.run("SELECT source,source_key FROM company_evidence_ref")
    )
    ReleaseUnusedCompanyDisclosures(connection, referenced)


def _release_obsolete(
    connection: SqliteConnection,
    raw_files: CompanyRawFiles,
    cutoff: datetime | None,
) -> None:
    obsolete = connection.run(
        "SELECT d.*,MIN(c.code) AS code,t.record_hash AS task_hash FROM document_tasks t JOIN disclosures d "
        "ON d.source=t.source AND d.source_key=t.source_key JOIN disclosure_codes c ON c.disclosure_id=d.id "
        "WHERE t.stage NOT IN ('cleaned','obsolete','expired') AND (t.record_hash<>d.record_hash OR d.published_at<?) "
        "AND NOT EXISTS(SELECT 1 FROM company_field_repairs f WHERE f.disclosure_id=d.id "
        "AND f.record_hash=t.record_hash AND f.state<>'complete') "
        "AND NOT EXISTS(SELECT 1 FROM company_material_dependencies m WHERE m.disclosure_id=d.id "
        "AND m.record_hash=t.record_hash) GROUP BY t.source,t.source_key,t.record_hash",
        (((cutoff.date() - timedelta(days=365)).isoformat() if cutoff else ""),),
    ).fetchall()
    for row in obsolete:
        item = replace(DecodeCompanyDisclosure(row), digest=row["task_hash"])
        if raw_files.release(item):
            connection.run(
                "UPDATE document_tasks SET stage=?,path=NULL WHERE source=? AND source_key=? AND record_hash=?",
                ("obsolete" if row["record_hash"] != item.digest else "expired", item.source, item.identity, item.digest),
            )


def _release_terminal_failures(
    connection: SqliteConnection,
    formal: SqliteConnection,
    raw_files: CompanyRawFiles,
    extraction_hash: bytes,
) -> None:
    failed = connection.run(
        "SELECT d.*,MIN(c.code) AS code,r.failure,r.retry_policy_id FROM document_tasks t JOIN disclosures d "
        "ON d.source=t.source AND d.source_key=t.source_key AND d.record_hash=t.record_hash "
        "JOIN disclosure_codes c ON c.disclosure_id=d.id JOIN document_retries r ON r.disclosure_id=d.id "
        # A receipt for an old attempt cannot release a new attempt's input.
        "AND r.record_hash=d.record_hash WHERE t.stage='failed' "
        "AND NOT EXISTS(SELECT 1 FROM company_material_dependencies m WHERE m.disclosure_id=d.id "
        "AND m.record_hash=t.record_hash) GROUP BY d.id"
    ).fetchall()
    for row in failed:
        if IsTransientCompanyDocumentFailure(row["failure"]):
            continue
        codes = tuple(c[0] for c in connection.run(
            "SELECT c.code FROM disclosure_codes c JOIN company_population u ON u.code=c.code WHERE c.disclosure_id=?",
            (row["id"],),
        ))
        if not codes or not all(formal.run(
            "SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? AND record_hash=? "
            "AND extraction_hash=? AND outcome=? AND policy_id=?",
            (row["source"], row["source_key"], code, row["record_hash"], extraction_hash,
             row["failure"], row["retry_policy_id"]),
        ).fetchone() is not None for code in codes):
            continue
        item = DecodeCompanyDisclosure(row)
        if raw_files.release(item):
            connection.run(
                "UPDATE document_tasks SET stage='cleaned',path=NULL WHERE source=? AND source_key=? AND record_hash=?",
                (item.source, item.identity, item.digest),
            )


__all__ = [
    "ReleasePublishedCompanyMaterial",
    "ReleaseCompanyMaterialFiles",
]
