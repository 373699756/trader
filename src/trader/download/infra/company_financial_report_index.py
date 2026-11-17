"""Plan bounded quarterly evidence requests from current structured financial gaps."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from trader.download.domain.company_evidence import Disclosure
from trader.download.domain.company_financial_report import CompanyFinancialReportRequest
from trader.infra.sqlite_connection import SqliteConnection


def _needs(collected: SqliteConnection, codes: Sequence[str], observed_at: datetime) -> tuple[CompanyFinancialReportRequest, ...]:
    requests: list[CompanyFinancialReportRequest] = []
    for offset in range(0, len(codes), 128):
        batch = codes[offset:offset + 128]
        slots = ",".join("?" for _ in batch)
        requests.extend(CompanyFinancialReportRequest(row[0], date.fromisoformat(row[1]), datetime.fromisoformat(row[2]))
                        for row in collected.run(
            f"SELECT f.code,f.report_date,f.published_at FROM financials f WHERE f.code IN ({slots}) "
            "AND substr(f.report_date,6,5)='09-30' AND f.published_at>=? AND f.published_at<=? "
            "AND (f.eps IS NULL OR f.roe_pct IS NULL OR f.core_profit_growth_pct IS NULL OR f.revenue_growth_pct IS NULL) "
            "AND NOT EXISTS(SELECT 1 FROM financials n WHERE n.code=f.code AND n.report_date>f.report_date AND n.published_at<=?)",
            (*batch, (observed_at.date() - timedelta(days=365)).isoformat(), observed_at.isoformat(), observed_at.isoformat()),
        ))
    return tuple(requests)


def PlanCompanyMissingFinancialReports(
    collected: SqliteConnection, codes: Sequence[str], observed_at: datetime,
) -> tuple[CompanyFinancialReportRequest, ...]:
    return tuple(request for request in _needs(collected, codes, observed_at) if collected.run(
        "SELECT 1 FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE c.code=? "
        "AND d.kind='announcement' AND d.published_at>=? AND d.published_at<=? "
        "AND d.source IN ('cninfo','sse','szse','bse') "
        "AND (SELECT COUNT(*) FROM disclosure_codes m WHERE m.disclosure_id=d.id)=1 "
        "AND (d.title LIKE ? OR d.title LIKE ? OR d.title LIKE ?) LIMIT 1",
        (request.code, max(observed_at.date() - timedelta(days=365), request.disclosed_at.date() - timedelta(days=1)).isoformat(),
         observed_at.isoformat(), f"%{request.report_date.year}%第三季度报告%",
         f"%{request.report_date.year}%三季度报告%", f"%{request.report_date.year}%三季报告%"),
    ).fetchone() is None)


def PrioritizeCompanyFinancialReportInputs(collected: SqliteConnection, codes: Sequence[str], observed_at: datetime) -> None:
    for request in _needs(collected, codes, observed_at):
        collected.run(
            "INSERT OR IGNORE INTO temp.company_field_inputs SELECT d.id FROM disclosures d "
            "JOIN disclosure_codes c ON c.disclosure_id=d.id JOIN temp.pending_documents p ON p.id=d.id "
            "WHERE c.code=? AND d.published_at>=? AND d.published_at<=? AND d.kind='announcement' "
            "AND d.source IN ('cninfo','sse','szse','bse') "
            "AND (SELECT COUNT(*) FROM disclosure_codes m WHERE m.disclosure_id=d.id)=1 "
            "AND (d.title LIKE ? OR d.title LIKE ? OR d.title LIKE ?)",
            (request.code, max(observed_at.date() - timedelta(days=365), request.disclosed_at.date() - timedelta(days=1)).isoformat(),
             observed_at.isoformat(), f"%{request.report_date.year}%第三季度报告%",
             f"%{request.report_date.year}%三季度报告%", f"%{request.report_date.year}%三季报告%"),
        )


def IndexCompanyFinancialReportInputs(collected: SqliteConnection, rows: Sequence[Disclosure]) -> None:
    for item in rows:
        period = re.search(r"(20[0-9]{2})年(?:第三季度|三季度|三季)报告", "".join(item.title.split()))
        if item.kind != "announcement" or period is None:
            continue
        if any(collected.run(
            "SELECT 1 FROM financials WHERE code=? AND report_date=? AND published_at<=? "
            "AND (eps IS NULL OR roe_pct IS NULL OR core_profit_growth_pct IS NULL OR revenue_growth_pct IS NULL)",
            (code, f"{period[1]}-09-30", item.published_at.isoformat()),
        ).fetchone() is not None for code in item.codes):
            collected.run(
                "INSERT OR IGNORE INTO company_document_requirements SELECT id FROM disclosures WHERE source=? AND source_key=?",
                (item.source, item.identity),
            )


__all__ = [
    "PlanCompanyMissingFinancialReports",
    "PrioritizeCompanyFinancialReportInputs",
    "IndexCompanyFinancialReportInputs",
]
