"""Select still-required company inputs from pinned collection and publication views."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime

from trader.download.domain.company_evidence import (
    CompanyDocumentFailureRules,
    CompanyDocumentPolicies,
    CompanyDocumentTask,
    Disclosure,
    DocumentRepairMode,
)
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_financial_input_use import ReadUnusedCompanyFinancialInputs
from trader.infra.sqlite_connection import SqliteConnection


def SelectCompanyDocuments(
    connection: SqliteConnection,
    formal: SqliteConnection,
    codes: Sequence[str],
    at: datetime,
    policies: CompanyDocumentPolicies,
    excluded: Sequence[tuple[str, str, str]],
    repair: DocumentRepairMode | None = None,
    *,
    start: date,
    extraction_hash: bytes,
    until: datetime | None = None,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    unused = ReadUnusedCompanyFinancialInputs(
        connection,
        formal,
        codes,
        until or at,
        extraction_hash,
        cancel_requested=cancel_requested,
    )
    connection.run("CREATE TEMP TABLE unused_financial_inputs(id INTEGER PRIMARY KEY)")
    connection.run_many("INSERT INTO temp.unused_financial_inputs VALUES(?)", ((identifier,) for identifier in unused))
    connection.run("CREATE TEMP TABLE requested_codes(code TEXT PRIMARY KEY) WITHOUT ROWID")
    connection.run_many("INSERT OR IGNORE INTO temp.requested_codes VALUES(?)", ((code,) for code in codes))
    connection.run(
        "CREATE TEMP TABLE attempted_documents(source TEXT,source_key TEXT,record_hash TEXT,PRIMARY KEY(source,source_key,record_hash)) WITHOUT ROWID"
    )
    connection.run_many("INSERT INTO temp.attempted_documents VALUES(?,?,?)", excluded)
    connection.run(
        "CREATE TEMP TABLE document_failure_rules(failure TEXT PRIMARY KEY,route TEXT NOT NULL,transient INTEGER NOT NULL) WITHOUT ROWID"
    )
    connection.run_many(
        "INSERT INTO temp.document_failure_rules VALUES(?,?,?)",
        ((failure, route, int(transient)) for failure, route, transient in CompanyDocumentFailureRules()),
    )
    connection.run(
        "CREATE TEMP TABLE document_policies(route TEXT PRIMARY KEY,identity TEXT NOT NULL) WITHOUT ROWID"
    )
    connection.run_many(
        "INSERT INTO temp.document_policies VALUES(?,?)",
        ((route, policies.for_route(route)) for route in ("standard", "ocr", "limits")),
    )
    repair_route = repair or ""
    connection.run(
        "CREATE TEMP TABLE pending_documents AS SELECT d.id,p.route,r.retry_at,"
        "CASE WHEN EXISTS(SELECT 1 FROM temp.attempted_documents a WHERE a.source=d.source AND a.source_key=d.source_key "
        "AND a.record_hash=d.record_hash) OR (?<>'' AND (r.disclosure_id IS NULL OR p.route<>?)) THEN 0 "
        "WHEN r.disclosure_id IS NULL OR r.retry_policy_id<>p.identity THEN 1 "
        "WHEN COALESCE(f.transient,0)=1 AND r.retry_at<=? THEN 1 ELSE 0 END AS due,"
        "CASE WHEN r.disclosure_id IS NOT NULL AND r.retry_policy_id=p.identity AND COALESCE(f.transient,0)=1 "
        "AND (r.retry_at>? OR EXISTS(SELECT 1 FROM temp.attempted_documents a WHERE a.source=d.source "
        "AND a.source_key=d.source_key AND a.record_hash=d.record_hash)) THEN 1 ELSE 0 END AS cooling,"
        "CASE WHEN p.route='ocr' THEN 1 ELSE 0 END AS ocr,"
        "CASE WHEN r.disclosure_id IS NOT NULL AND r.retry_policy_id=p.identity AND COALESCE(f.transient,0)=0 "
        "THEN 1 ELSE 0 END AS unextractable "
        "FROM company_document_requirements q JOIN disclosures d ON d.id=q.disclosure_id "
        "LEFT JOIN document_retries r ON r.disclosure_id=d.id AND r.record_hash=d.record_hash "
        "LEFT JOIN temp.document_failure_rules f ON f.failure=r.failure "
        "JOIN temp.document_policies p ON p.route=COALESCE(f.route,'standard') "
        "WHERE d.body_checked=0 AND d.published_at>=? AND d.published_at<=? "
        "AND NOT EXISTS(SELECT 1 FROM temp.unused_financial_inputs f WHERE f.id=d.id) "
        "AND (?='' OR (r.disclosure_id IS NOT NULL AND p.route=?)) "
        "AND EXISTS(SELECT 1 FROM disclosure_codes c JOIN temp.requested_codes p ON p.code=c.code WHERE c.disclosure_id=d.id "
        "AND NOT EXISTS(SELECT 1 FROM document_supersessions s JOIN disclosures n ON n.id=s.replacement_id "
        "JOIN document_publications r ON r.disclosure_id=n.id AND r.code=c.code AND r.record_hash=n.record_hash "
        "AND r.extraction_hash=s.extraction_hash WHERE s.disclosure_id=d.id AND s.code=c.code AND s.record_hash=d.record_hash "
        "AND s.replacement_hash=n.record_hash AND s.extraction_hash=?))",
        (
            repair_route,
            repair_route,
            at.isoformat(),
            at.isoformat(),
            start.isoformat(),
            (until or at).isoformat(),
            repair_route,
            repair_route,
            extraction_hash,
        ),
    )
    _check_cancelled(cancel_requested)


def ReadPendingCompanyDocuments(
    connection: SqliteConnection,
    *,
    limit: int | None,
    due_only: bool,
    local_only: bool,
) -> tuple[CompanyDocumentTask, ...]:
    rows = connection.run(
        "SELECT d.source,d.source_key,d.kind,d.title,d.published_at,d.url,d.record_hash,c.code,p.route FROM disclosures d "
        "JOIN disclosure_codes c ON c.disclosure_id=d.id JOIN temp.requested_codes u ON u.code=c.code "
        "JOIN temp.pending_documents p ON p.id=d.id WHERE d.id IN ("
        "SELECT selected.id FROM temp.pending_documents selected JOIN disclosures s ON s.id=selected.id "
        "WHERE (?=0 OR selected.due=1) AND (?=0 OR EXISTS(SELECT 1 FROM document_tasks t "
        "WHERE t.source=s.source AND t.source_key=s.source_key AND t.record_hash=s.record_hash AND t.path IS NOT NULL)) "
        "ORDER BY EXISTS(SELECT 1 FROM temp.company_field_inputs f WHERE f.id=s.id) DESC,"
        "s.published_at DESC,EXISTS(SELECT 1 FROM document_tasks t "
        "WHERE t.source=s.source AND t.source_key=s.source_key AND t.record_hash=s.record_hash AND t.path IS NOT NULL) DESC,"
        "s.id LIMIT ?) "
        "ORDER BY EXISTS(SELECT 1 FROM temp.company_field_inputs f WHERE f.id=d.id) DESC,d.published_at DESC,d.id,c.code",
        (due_only, local_only, -1 if limit is None else limit),
    ).fetchall()
    items: dict[tuple[str, str], CompanyDocumentTask] = {}
    for row in rows:
        item = DecodeCompanyDisclosure(row)
        key = item.source, item.identity
        previous = items.get(key)
        items[key] = (
            CompanyDocumentTask(item, row[8])
            if previous is None
            else CompanyDocumentTask(replace(previous.disclosure, codes=(*previous.disclosure.codes, *item.codes)), previous.route)
        )
    return tuple(items.values())


def CountCompanyUnpublishedBodies(connection: SqliteConnection, cutoff: datetime, extraction_hash: bytes) -> int:
    """Count accepted inputs using the same still-required set as dispatch and workload."""
    return int(connection.run(
        "SELECT COUNT(*) FROM company_document_requirements q JOIN disclosures d ON d.id=q.disclosure_id "
        "WHERE d.body_checked=1 AND d.body_retained=1 AND d.published_at<=? "
        "AND NOT EXISTS(SELECT 1 FROM temp.unused_financial_inputs f WHERE f.id=d.id) "
        "AND EXISTS(SELECT 1 FROM disclosure_codes c JOIN temp.requested_codes u ON u.code=c.code "
        "WHERE c.disclosure_id=d.id AND NOT EXISTS(SELECT 1 FROM document_publications p "
        "WHERE p.disclosure_id=d.id AND p.code=c.code AND p.record_hash=d.record_hash AND p.extraction_hash=?) "
        "AND NOT EXISTS(SELECT 1 FROM document_supersessions s JOIN disclosures n ON n.id=s.replacement_id "
        "JOIN document_publications p ON p.disclosure_id=n.id AND p.code=c.code AND p.record_hash=n.record_hash "
        "AND p.extraction_hash=s.extraction_hash WHERE s.disclosure_id=d.id AND s.code=c.code "
        "AND s.record_hash=d.record_hash AND s.replacement_hash=n.record_hash AND s.extraction_hash=?))",
        (cutoff.isoformat(), extraction_hash, extraction_hash),
    ).require_one()[0])


def IsCompanyDocumentRequired(
    connection: SqliteConnection,
    formal: SqliteConnection,
    item: Disclosure,
    observed_at: datetime,
    extraction_hash: bytes,
    *,
    cancel_requested: Callable[[], bool] | None,
) -> bool:
    rows = connection.run(
        "SELECT d.id FROM disclosures d JOIN company_document_requirements q ON q.disclosure_id=d.id "
        "JOIN disclosure_codes c ON c.disclosure_id=d.id "
        "JOIN company_population u ON u.code=c.code WHERE d.source=? AND d.source_key=? AND d.record_hash=? "
        "AND d.body_checked=0 AND NOT EXISTS(SELECT 1 FROM document_supersessions s "
        "JOIN disclosures n ON n.id=s.replacement_id AND n.record_hash=s.replacement_hash "
        "JOIN document_publications p ON p.disclosure_id=n.id AND p.code=c.code AND p.record_hash=n.record_hash "
        "AND p.extraction_hash=s.extraction_hash WHERE s.disclosure_id=d.id AND s.code=c.code "
        "AND s.record_hash=d.record_hash AND s.extraction_hash=?)",
        (item.source, item.identity, item.digest, extraction_hash),
    ).fetchall()
    unused = ReadUnusedCompanyFinancialInputs(
        connection,
        formal,
        item.codes,
        observed_at,
        extraction_hash,
        cancel_requested=cancel_requested,
    )
    _check_cancelled(cancel_requested)
    return any(row[0] not in unused for row in rows)


def _check_cancelled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise InterruptedError("company_document_selection_cancelled")


__all__ = [
    "CountCompanyUnpublishedBodies",
    "IsCompanyDocumentRequired",
    "ReadPendingCompanyDocuments",
    "SelectCompanyDocuments",
]
