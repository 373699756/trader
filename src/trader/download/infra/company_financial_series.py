"""Durable report order, market-prefix closure and exact issuer receipts for latest financials."""

from dataclasses import dataclass, replace
from datetime import date, datetime
import json

from trader.download.domain.company_evidence import CompanyInputValidationFailure, CompanyLatestFinancialQuery
from trader.download.domain.company_source_records import CompanyFinancialOrderIncreases, CompanyFinancialSourcePage
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteConnection


def CompanyLatestFinancialScope(query: CompanyLatestFinancialQuery, codes: tuple[str, ...], cutoff: datetime) -> str:
    return CanonicalDigest(("latest_financial_by_report", query.start, query.end, codes, cutoff))


def CompanyFinancialPageScope(scope: str, query: CompanyLatestFinancialQuery) -> str:
    return CanonicalDigest((scope, "exact_issuers", query.securities)) if query.securities else scope


@dataclass(frozen=True, slots=True)
class _Group:
    accepted: int
    declared: int | None
    rows: int
    declared_rows: int | None
    last_key: tuple[date, str] | None
    receipts: tuple[tuple[int, str], ...]

    @property
    def complete(self) -> bool:
        return self.accepted == self.declared


def _read_group(connection: SqliteConnection, scope: str) -> _Group:
    results = connection.run("""SELECT p.page,p.pages,p.total,p.raw_count,p.response_hash,t.task
        FROM company_round_page p LEFT JOIN company_source_task t ON t.scope=p.scope AND t.family='financial_order'
        AND t.accepted=p.page AND t.input_hash=p.response_hash WHERE p.scope=? ORDER BY p.page""", (scope,)).fetchall()
    if not results:
        return _Group(0, None, 0, None, None, ())
    declared, total = results[0][1:3]
    count = 0
    last_key = None
    receipts = []
    for number, row in enumerate(results, 1):
        page, pages, expected, raw, digest, task = row
        if (page != number or pages != declared or expected != total or task is None
            or pages != max(1, (total + 499) // 500) or raw != min(500, total - count)):
            raise CompanyInputValidationFailure("company_financial_group_count_mismatch")
        cursor_page, day, security = json.loads(task)
        if cursor_page != page or ((day is None) != (security is None)):
            raise CompanyInputValidationFailure("company_financial_group_order_missing")
        last_key = (date.fromisoformat(day), security) if day is not None else None
        count += raw
        receipts.append((page, digest))
    return _Group(len(results), declared, count, total, last_key, tuple(receipts))


@dataclass(frozen=True, slots=True)
class CompanyFinancialSeriesState:
    market_read_pages: int
    market_declared_pages: int | None
    market_read_rows: int
    market_declared_rows: int | None
    accepted_pages: int
    expected_pages: int | None
    accepted_raw_rows: int
    expected_raw_rows: int | None
    exact_codes: tuple[str, ...]
    cursor: CompanyLatestFinancialQuery | None
    watermark: str | None

    @property
    def complete(self) -> bool:
        return self.watermark is not None


def _selection(connection: SqliteConnection, root: str, market: _Group) -> tuple[str, tuple[str, ...]] | None:
    rows = connection.run("SELECT task,accepted,input_hash FROM company_source_task WHERE scope=? AND family='financial_prefix'", (root,)).fetchall()
    if not rows:
        if market.complete:
            raise CompanyInputValidationFailure("company_financial_prefix_receipt_missing")
        return None
    if len(rows) != 1 or not market.receipts or (rows[0][1], rows[0][2]) != market.receipts[-1]:
        raise CompanyInputValidationFailure("company_financial_prefix_receipt_mismatch")
    witness, targets = json.loads(rows[0][0])
    if (bool(witness) != bool(targets) or witness in targets or targets != sorted(set(targets))
        or len(targets) > 7 or market.complete and targets):
        raise CompanyInputValidationFailure("company_financial_prefix_targets_invalid")
    return witness, tuple(targets)


def ReadCompanyLatestFinancialSeries(
    connection: SqliteConnection, query: CompanyLatestFinancialQuery, codes: tuple[str, ...], cutoff: datetime,
) -> CompanyFinancialSeriesState:
    root = CompanyLatestFinancialScope(query, codes, cutoff)
    market = _read_group(connection, root)
    selection = _selection(connection, root, market)
    targets: tuple[str, ...] = ()
    members: tuple[str, ...] = ()
    group = market
    accepted, raw = market.accepted, market.rows
    expected = expected_raw = None
    watermark = None
    if selection is not None:
        witness, targets = selection
        if targets:
            members = tuple(sorted((witness, *targets)))
            group = _read_group(connection, CompanyFinancialPageScope(root, replace(query, securities=members)))
            accepted += group.accepted
            raw += group.rows
            if group.declared is not None and group.declared_rows is not None:
                expected, expected_raw = market.accepted + group.declared, market.rows + group.declared_rows
        if not targets or group.complete:
            expected, expected_raw = accepted, raw
            watermark = CanonicalDigest((root, selection, market.receipts, group.receipts if targets else ()))
    cursor = None if watermark else replace(query, securities=members, page=group.accepted + 1,
                                           expected_rows=group.declared_rows, received_rows=group.rows)
    return CompanyFinancialSeriesState(market.accepted, market.declared, market.rows, market.declared_rows,
        accepted, expected, raw, expected_raw, targets, cursor, watermark)


def RecordCompanyFinancialSeriesPage(
    connection: SqliteConnection, root: str, group: str, query: CompanyLatestFinancialQuery,
    page: CompanyFinancialSourcePage, codes: tuple[str, ...],
) -> None:
    """Called inside the same transaction as this page's observations and raw response receipt."""
    key = page.last_order_key
    if (key is None and page.raw_count and page.pages > 1
        or page.pages != max(1, (page.total + 499) // 500)
        or page.raw_count != min(500, page.total - query.received_rows)):
        raise CompanyInputValidationFailure("company_financial_group_count_mismatch")
    if page.page > 1:
        prior = connection.run("SELECT task FROM company_source_task WHERE scope=? AND family='financial_order' AND accepted=?",
                               (group, page.page - 1)).require_one()
        _, day, security = json.loads(prior[0])
        if (not day or page.first_order_key is None or not CompanyFinancialOrderIncreases(
                (date.fromisoformat(day), security), page.first_order_key, exact=bool(query.securities))):
            raise CompanyInputValidationFailure("company_financial_group_order_changed")
    metadata = (page.page, key[0].isoformat() if key else None, key[1] if key else None)
    connection.run("INSERT INTO company_source_task VALUES(?,?,?,?,?)",
                   (group, "financial_order", json.dumps(metadata), page.page, page.response_hash))
    if query.securities:
        _record_exact_members(connection, root, group, page)
        return
    witness = ""
    missing: tuple[str, ...] = ()
    if page.page < page.pages:
        assert key is not None
        # Every record in a strictly newer report period has been read, including corrections.
        # The period on the current tail can still continue onto the next page.
        closed = {row[0] for row in connection.run(
            "SELECT DISTINCT code FROM company_round_member WHERE scope=? AND report_date>?", (root, key[0].isoformat()))}
        covered = closed.intersection(codes)
        missing = tuple(code for code in codes if code not in covered)
        if not covered or len(missing) > 7 or missing and page.page + 1 == page.pages:
            return
        if missing:
            witness_row = connection.run("SELECT code FROM company_round_member WHERE scope=? AND report_date>? "
                "AND record_hash IS NOT NULL ORDER BY code LIMIT 1", (root, key[0].isoformat())).fetchone()
            if witness_row is None:
                return
            witness = witness_row[0]
    connection.run("INSERT INTO company_source_task VALUES(?,?,?,?,?)",
                   (root, "financial_prefix", json.dumps((witness, missing)), page.page, page.response_hash))


def _record_exact_members(connection: SqliteConnection, root: str, group: str, page: CompanyFinancialSourcePage) -> None:
    market = _read_group(connection, root)
    selection = _selection(connection, root, market)
    if selection is None or not selection[1]:
        raise CompanyInputValidationFailure("company_financial_exact_scope_missing")
    witness, targets = selection
    boundary = market.last_key
    assert boundary is not None
    for observation in page.observations:
        code, day = observation.business_key
        if code not in (witness, *targets):
            raise CompanyInputValidationFailure("company_financial_exact_scope_changed")
        existing = connection.run("SELECT record_hash FROM company_round_member WHERE scope=? AND code=? AND report_date=?",
                                  (root, code, day.isoformat())).fetchone()
        if ((day > boundary[0] and existing is None)
            or existing is not None and existing[0] is not None and existing[0] != observation.record_hash):
            raise CompanyInputValidationFailure("company_financial_exact_version_changed")
        if code in targets:
            connection.run("INSERT OR IGNORE INTO company_round_member SELECT ?,code,report_date,record_hash "
                "FROM company_round_member WHERE scope=? AND code=? AND report_date=?", (root, group, code, day.isoformat()))
        if existing is not None and existing[0] is not None and existing[0] == observation.record_hash:
            connection.run("INSERT OR IGNORE INTO company_source_task VALUES(?,?,?,?,?)",
                           (group, "financial_overlap", f"{code}:{day.isoformat()}", 1, observation.record_hash))
    if page.page == page.pages:
        members = (witness, *targets)
        slots = ",".join("?" for _ in members)
        unconfirmed = connection.run(f"""SELECT 1 FROM company_round_member m WHERE m.scope=?
            AND m.code IN ({slots}) AND m.record_hash IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM company_round_member g WHERE g.scope=? AND g.code=m.code
            AND g.report_date=m.report_date AND g.record_hash=m.record_hash) AND NOT EXISTS (
            SELECT 1 FROM company_source_task t WHERE t.scope=? AND t.family='financial_overlap'
            AND t.task=m.code||':'||m.report_date AND t.input_hash=m.record_hash) LIMIT 1""",
            (root, *members, group, group)).fetchone()
        if unconfirmed is not None:
            raise CompanyInputValidationFailure("company_financial_exact_witness_missing")


__all__ = ["CompanyFinancialSeriesState", "CompanyLatestFinancialScope", "CompanyFinancialPageScope",
           "ReadCompanyLatestFinancialSeries", "RecordCompanyFinancialSeriesPage"]
