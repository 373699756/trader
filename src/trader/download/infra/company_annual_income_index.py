"""Collection ownership of annual income observations and targeted requests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from trader.download.domain.company_annual_income import CompanyAnnualIncome, CompanyAnnualIncomeRequest
from trader.download.domain.company_events import CompanyPositiveStatus, ContractMeasurements
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.infra.sqlite_connection import SqliteConnection


def EnsureCompanyAnnualIncome(connection: SqliteConnection) -> None:
    connection.run(
        "CREATE TABLE IF NOT EXISTS annual_income("
        "code TEXT NOT NULL,report_date TEXT NOT NULL,disclosed_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
        "observed_at TEXT NOT NULL,currency TEXT NOT NULL,operating_revenue REAL,record_hash TEXT NOT NULL,"
        "PRIMARY KEY(code,report_date,disclosed_at,updated_at,record_hash)) WITHOUT ROWID"
    )


def ReadCompanyAnnualIncome(
    connection: SqliteConnection, codes: Sequence[str], observed_at: datetime
) -> tuple[CompanyAnnualIncome, ...]:
    if not codes:
        return ()
    placeholders = ",".join("?" for _ in codes)
    return tuple(
        CompanyAnnualIncome(
            row[0], date.fromisoformat(row[1]), datetime.fromisoformat(row[2]), datetime.fromisoformat(row[3]),
            datetime.fromisoformat(row[4]), row[5], row[6], row[7],
        )
        for row in connection.run(
            f"SELECT * FROM annual_income WHERE code IN ({placeholders}) AND observed_at<=? "
            "ORDER BY code,report_date,updated_at,record_hash", (*codes, observed_at.isoformat()),
        )
    )


def PlanCompanyAnnualIncomeRequests(
    collected: SqliteConnection,
    formal: SqliteConnection,
    codes: Sequence[str],
    observed_at: datetime,
) -> tuple[CompanyAnnualIncomeRequest, ...]:
    """Only request known, in-window annual disclosures needed by published contracts."""
    requests: list[CompanyAnnualIncomeRequest] = []
    for offset in range(0, len(codes), 128):
        batch = codes[offset:offset + 128]
        placeholders = ",".join("?" for _ in batch)
        candidates = tuple(row[0] for row in formal.run(
            f"SELECT DISTINCT code FROM company_positive_event WHERE code IN ({placeholders}) "
            "AND revenue_report_date IS NOT NULL AND (annual_revenue IS NULL OR revenue_disclosed_at IS NULL)", batch,
        ))
        cached = ReadCompanyAnnualIncome(collected, candidates, observed_at)
        for facts in ReadCompanyFactRows(formal, candidates):
            dates: set[date] = set()
            for event in facts.positive_events:
                measurement = event.measurements
                if event.status is CompanyPositiveStatus.CANCELLED or not isinstance(measurement, ContractMeasurements) or (
                    measurement.reference_annual_revenue_cny is not None
                    and measurement.reference_disclosed_at is not None
                ) or measurement.reference_report_date is None:
                    continue
                annuals = tuple(
                    (date.fromisoformat(row[0]), datetime.fromisoformat(row[1]))
                    for row in collected.run(
                        "SELECT report_date,published_at FROM financials WHERE code=? AND substr(report_date,6,5)='12-31' "
                        "AND published_at>=? AND published_at<? ORDER BY report_date DESC,published_at DESC",
                        (facts.code, (observed_at.date() - timedelta(days=365)).isoformat(), event.first_disclosed_at.date().isoformat()),
                    )
                    if date.fromisoformat(row[0]) == measurement.reference_report_date
                )
                if not annuals:
                    continue
                report_date, disclosed_at = annuals[0]
                if any(
                    income.code == facts.code and income.report_date == report_date
                    and income.observed_at >= event.first_disclosed_at and income.updated_at >= disclosed_at for income in cached
                ):
                    continue
                dates.add(report_date)
            ordered = tuple(sorted(dates))
            requests.extend(CompanyAnnualIncomeRequest(facts.code, ordered[start:start + 5]) for start in range(0, len(ordered), 5))
    return tuple(requests)


def WriteCompanyAnnualIncome(
    connection: SqliteConnection,
    request: CompanyAnnualIncomeRequest,
    incomes: tuple[CompanyAnnualIncome, ...],
    observed_at: datetime,
) -> int:
    if connection.run("SELECT 1 FROM company_population WHERE code=?", (request.code,)).fetchone() is None:
        raise ValueError("company_annual_income_population_mismatch")
    if {row.report_date for row in incomes} != set(request.report_dates) or len(incomes) != len(request.report_dates):
        raise ValueError("company_annual_income_reports_mismatch")
    for income in incomes:
        if income.code != request.code or income.observed_at != observed_at:
            raise ValueError("company_annual_income_identity_mismatch")
        known = connection.run(
            "SELECT 1 FROM financials WHERE code=? AND report_date=? AND published_at>=? AND published_at<=?",
            (income.code, income.report_date.isoformat(), (observed_at.date() - timedelta(days=365)).isoformat(), observed_at.isoformat()),
        ).fetchone()
        if known is None:
            raise ValueError("company_annual_income_disclosure_unavailable")
    before = connection.total_changes
    connection.run_many(
        "INSERT INTO annual_income VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(code,report_date,disclosed_at,updated_at,record_hash) DO UPDATE SET "
        "observed_at=excluded.observed_at WHERE observed_at<excluded.observed_at",
        ((row.code, row.report_date.isoformat(), row.disclosed_at.isoformat(), row.updated_at.isoformat(),
          row.observed_at.isoformat(), row.currency, row.operating_revenue, row.record_hash) for row in incomes),
    )
    return connection.total_changes - before


__all__ = [
    "EnsureCompanyAnnualIncome",
    "PlanCompanyAnnualIncomeRequests",
    "ReadCompanyAnnualIncome",
    "WriteCompanyAnnualIncome",
]
