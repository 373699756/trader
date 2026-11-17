"""One relational codec for source financial identities in collection and publication."""

from collections.abc import Sequence
from datetime import date, datetime

from trader.download.domain.company_evidence import CompanyInputValidationFailure
from trader.download.domain.company_evidence import CompanyFinancial
from trader.download.domain.company_source_records import (
    CompanyFinancialMissingField, CompanyFinancialSourceObservation, CompanyFinancialSourceValues,
)
from trader.infra.sqlite_connection import SqliteConnection, SqliteSchema


COMPANY_FINANCIAL_OBSERVATIONS_SCHEMA = SqliteSchema((
    """CREATE TABLE company_financial_observation (
      code TEXT NOT NULL,report_date TEXT NOT NULL,disclosed_at TEXT NOT NULL,
      eps REAL,bps REAL,revenue_growth_pct REAL,profit_growth_pct REAL,core_profit_growth_pct REAL,
      roe_pct REAL,parent_profit REAL,core_profit REAL,security_identity TEXT NOT NULL,
      notice_at TEXT NOT NULL,updated_at TEXT,observed_at TEXT NOT NULL,currency TEXT NOT NULL,
      record_hash TEXT NOT NULL,PRIMARY KEY(code,report_date)
    ) WITHOUT ROWID""",
))


def WriteCompanyFinancialObservations(
    connection: SqliteConnection,
    observations: Sequence[CompanyFinancialSourceObservation],
) -> None:
    for observation in observations:
        financial = observation.financial
        prior = connection.run(
            "SELECT record_hash FROM company_financial_observation WHERE code=? AND report_date=?",
            (financial.code, financial.report_date.isoformat()),
        ).fetchone()
        if prior is not None and prior[0] != observation.record_hash:
            raise CompanyInputValidationFailure("company_normalization_revision_unproven")
        connection.run(
            "INSERT OR IGNORE INTO company_financial_observation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (financial.code, financial.report_date.isoformat(), financial.published_at.isoformat(),
             *(value for _, value in CompanyFinancialSourceValues(financial)), observation.source_security_identity,
             observation.source_notice_at.isoformat(),
             observation.source_updated_at.isoformat() if observation.source_updated_at else None,
             observation.observed_at.isoformat(), observation.currency, observation.record_hash),
        )


def ReadCompanyFinancialObservations(
    connection: SqliteConnection,
    codes: Sequence[str],
) -> tuple[CompanyFinancialSourceObservation, ...]:
    result = []
    for offset in range(0, len(codes), 128):
        batch = codes[offset:offset + 128]
        slots = ",".join("?" for _ in batch)
        for row in connection.run(
            f"SELECT * FROM company_financial_observation WHERE code IN ({slots}) ORDER BY code,report_date", batch,
        ):
            financial = CompanyFinancial(row[0], date.fromisoformat(row[1]), datetime.fromisoformat(row[2]), *tuple(row)[3:11])
            result.append(CompanyFinancialSourceObservation(
                financial, row[11], datetime.fromisoformat(row[12]), datetime.fromisoformat(row[13]) if row[13] else None,
                datetime.fromisoformat(row[14]), row[15], row[16],
                tuple(CompanyFinancialMissingField(field) for field, value in CompanyFinancialSourceValues(financial) if value is None),
            ))
    return tuple(result)


__all__ = [
    "COMPANY_FINANCIAL_OBSERVATIONS_SCHEMA", "WriteCompanyFinancialObservations", "ReadCompanyFinancialObservations",
]
