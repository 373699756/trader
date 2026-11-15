"""Retain report inputs unless a committed current report ends their sole financial use."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
import json
import re

from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_evidence import CompanyFinancial
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import EVIDENCE_SOURCES
from trader.infra.sqlite_connection import SqliteConnection

_QUARTER_REPORT_TITLE = re.compile(r"(20[0-9]{2})年(?:第三季度|三季度|三季)报告(?:全文|正文)?")


def ReadUnusedCompanyFinancialInputs(
    collected: SqliteConnection,
    formal: SqliteConnection,
    codes: Sequence[str],
    observed_at: datetime,
    extraction_hash: bytes,
    *,
    cancel_requested: Callable[[], bool] | None,
) -> frozenset[int]:
    """Use caller-pinned views; this decision neither completes nor deletes an input."""
    _check_cancelled(cancel_requested)
    parent = ReadCompanyPublicationIdentity(formal)
    if parent is None or parent.extraction_hash != extraction_hash or parent.published_at > observed_at:
        return frozenset()
    source_version = collected.run("PRAGMA data_version").require_one()[0]
    unused: set[int] = set()
    for offset in range(0, len(codes), 128):
        _check_cancelled(cancel_requested)
        batch = tuple(codes[offset:offset + 128])
        current = {
            fact.code: fact.financial
            for fact in ReadCompanyFactRows(formal, batch)
            if fact.financial_state is CompanyDataState.COMPLETE
            and fact.financial is not None and fact.financial == fact.latest_financial
            and fact.financial.published_at <= fact.published_at <= parent.published_at
            and fact.published_at <= observed_at
            and fact.financial.report_date <= observed_at.date()
            and fact.financial.published_at <= observed_at
            and all(value is not None for value in _financial_values(fact.financial))
        }
        for code, report in tuple(current.items()):
            raw = collected.run(
                "SELECT code,report_date,published_at,eps,bps,revenue_growth_pct,profit_growth_pct,"
                "core_profit_growth_pct,roe_pct,parent_profit,core_profit FROM financials "
                "WHERE code=? AND report_date<=? AND published_at<=? "
                "ORDER BY report_date DESC,published_at DESC LIMIT 1",
                (code, observed_at.date().isoformat(), observed_at.isoformat()),
            ).fetchone()
            if raw is None or (
                raw[0], raw[1], datetime.fromisoformat(raw[2]), *tuple(raw)[3:]
            ) != (report.code, report.report_date.isoformat(), report.published_at, *_financial_values(report)):
                del current[code]
        if not current:
            continue
        slots = ",".join("?" for _ in current)
        rows = collected.run(
            "SELECT d.id,d.source,d.source_key,d.title,d.published_at,c.code FROM disclosures d "
            "JOIN company_document_requirements q ON q.disclosure_id=d.id "
            "JOIN disclosure_codes c ON c.disclosure_id=d.id "
            f"WHERE c.code IN ({slots}) AND d.kind='announcement' AND d.published_at<=? "
            "AND (SELECT COUNT(*) FROM disclosure_codes m WHERE m.disclosure_id=d.id)=1 "
            "AND NOT EXISTS(SELECT 1 FROM company_material_dependencies m WHERE m.disclosure_id=d.id) "
            "AND NOT EXISTS(SELECT 1 FROM company_field_repairs r WHERE r.disclosure_id=d.id AND r.state<>'complete') "
            "AND NOT EXISTS(SELECT 1 FROM document_link_candidates l WHERE l.source=d.source AND l.source_key=d.source_key) "
            "AND NOT EXISTS(SELECT 1 FROM document_contract_link_rules l WHERE l.source=d.source AND l.source_key=d.source_key) "
            "AND NOT EXISTS(SELECT 1 FROM document_supersessions s WHERE s.disclosure_id=d.id OR s.replacement_id=d.id) "
            "AND NOT EXISTS(SELECT 1 FROM document_announcement_numbers n WHERE n.disclosure_id=d.id)",
            (*current, observed_at.isoformat()),
        )
        for row in rows:
            _check_cancelled(cancel_requested)
            title = "".join(row[3].split())
            matched = _QUARTER_REPORT_TITLE.fullmatch(title)
            if matched is None or CompanyDocumentNeedsReview(row[3]) or row[1] not in EVIDENCE_SOURCES:
                continue
            period = date(int(matched[1]), 9, 30)
            report = current[row[5]]
            disclosed_at = datetime.fromisoformat(row[4])
            if (period >= report.report_date or period >= disclosed_at.date()
                or disclosed_at > report.published_at):
                continue
            if collected.run(
                "SELECT 1 FROM document_link_candidates WHERE instr(payload,?)>0 LIMIT 1",
                (json.dumps(row[2], ensure_ascii=False),),
            ).fetchone() is not None:
                continue
            if _has_formal_use(formal, row[1], row[2], row[5]):
                continue
            unused.add(row[0])
    _check_cancelled(cancel_requested)
    if (ReadCompanyPublicationIdentity(formal) != parent
        or collected.run("PRAGMA data_version").require_one()[0] != source_version):
        return frozenset()
    _check_cancelled(cancel_requested)
    return frozenset(unused)


def _financial_values(report: CompanyFinancial) -> tuple[float | None, ...]:
    return (
        report.basic_eps, report.book_value_per_share, report.revenue_growth_pct, report.net_profit_growth_pct,
        report.core_profit_growth_pct, report.roe_pct, report.parent_net_profit, report.core_net_profit,
    )


def _has_formal_use(formal: SqliteConnection, source: str, source_key: str, code: str) -> bool:
    return any(
        formal.run(query, parameters).fetchone() is not None
        for query, parameters in (
            ("SELECT 1 FROM company_evidence_ref WHERE source=? AND source_key=? LIMIT 1",
             (EVIDENCE_SOURCES[source], source_key)),
            ("SELECT 1 FROM company_field_evidence WHERE source=? AND source_key=? LIMIT 1", (source, source_key)),
            ("SELECT 1 FROM company_input_receipt WHERE source=? AND source_key=? AND code=? LIMIT 1",
             (source, source_key, code)),
            ("SELECT 1 FROM company_exclusion WHERE source_key=? LIMIT 1", (source_key,)),
        )
    )


def _check_cancelled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise InterruptedError("company_financial_input_use_cancelled")


__all__ = ["ReadUnusedCompanyFinancialInputs"]
