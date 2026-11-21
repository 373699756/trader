"""One relational codec for source financial identities in collection and publication."""

from collections.abc import Sequence
from datetime import date, datetime
from sqlite3 import Row

from trader.download.domain.company_evidence import CompanyFinancial
from trader.download.domain.company_source_records import (
    CompanyFinancialMissingField, CompanyFinancialSourceObservation, CompanyFinancialSourceValues,
)
from trader.download.infra.company_facts_protocol import EncodeCompanyTime
from trader.infra.sqlite_connection import SqliteConnection, SqliteSchema


COMPANY_FINANCIAL_OBSERVATIONS_SCHEMA = SqliteSchema((
    """CREATE TABLE company_financial_observation (
      code TEXT NOT NULL,report_date TEXT NOT NULL,disclosed_at TEXT NOT NULL,
      eps REAL,bps REAL,revenue_growth_pct REAL,profit_growth_pct REAL,core_profit_growth_pct REAL,
      roe_pct REAL,parent_profit REAL,core_profit REAL,security_identity TEXT NOT NULL,
      notice_at TEXT NOT NULL,updated_at TEXT,observed_at TEXT NOT NULL,currency TEXT NOT NULL,
      record_hash TEXT NOT NULL,PRIMARY KEY(code,report_date,record_hash)
    ) WITHOUT ROWID""",
))


def WriteCompanyFinancialObservations(
    connection: SqliteConnection,
    observations: Sequence[CompanyFinancialSourceObservation],
) -> None:
    for observation in observations:
        financial = observation.financial
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
    result: list[CompanyFinancialSourceObservation] = []
    for offset in range(0, len(codes), 128):
        batch = codes[offset:offset + 128]
        slots = ",".join("?" for _ in batch)
        result.extend(_decode(row) for row in connection.run(
            f"SELECT * FROM company_financial_observation WHERE code IN ({slots}) ORDER BY code,report_date,record_hash", batch,
        ))
    return tuple(result)


def ReadRoundCompanyFinancialObservations(
    connection: SqliteConnection,
    scope: str,
) -> tuple[CompanyFinancialSourceObservation, ...]:
    return tuple(_decode(row) for row in connection.run(
        "SELECT o.* FROM company_round_member m JOIN company_financial_observation o "
        "ON o.code=m.code AND o.report_date=m.report_date AND o.record_hash=m.record_hash "
        "WHERE m.scope=? ORDER BY m.code,m.report_date,m.record_hash",
        (scope,),
    ))


def ReadCommittedCompanyFinancialReports(
    connection: SqliteConnection,
    codes: Sequence[str],
    cutoff: datetime,
    extraction_hash: bytes,
) -> tuple[CompanyFinancial, ...]:
    """Read only observed inputs with exact, visible formal receipts."""
    reports: dict[tuple[str, date], set[CompanyFinancial]] = {}
    for offset in range(0, len(codes), 128):
        batch = codes[offset:offset + 128]
        slots = ",".join("?" for _ in batch)
        rows = connection.run(
            "SELECT o.* FROM company_financial_observation o WHERE "
            f"o.code IN ({slots}) AND EXISTS (SELECT 1 FROM company_input_receipt r "
            "WHERE r.source='eastmoney_financial' AND r.source_key=o.security_identity||':'||o.report_date "
            "AND r.code=o.code AND r.record_hash=o.record_hash AND r.extraction_hash=? "
            "AND r.published_at<=? AND r.outcome='valid' AND r.body_hash IS NULL AND r.policy_id='') "
            "ORDER BY o.code,o.report_date,o.record_hash",
            (*batch, extraction_hash, EncodeCompanyTime(cutoff)),
        )
        for row in rows:
            observation = _decode(row)
            if observation.observed_at <= cutoff:
                reports.setdefault(observation.business_key, set()).add(observation.financial)
    return tuple(next(iter(reports[key])) for key in sorted(reports) if len(reports[key]) == 1)


def _decode(row: Row) -> CompanyFinancialSourceObservation:
    financial = CompanyFinancial(
        row[0], date.fromisoformat(row[1]), datetime.fromisoformat(row[2]), *row[3:11],
    )
    return CompanyFinancialSourceObservation(
        financial, row[11], datetime.fromisoformat(row[12]), datetime.fromisoformat(row[13]) if row[13] else None,
        datetime.fromisoformat(row[14]), row[15], row[16],
        tuple(CompanyFinancialMissingField(field) for field, value in CompanyFinancialSourceValues(financial) if value is None),
    )


__all__ = [
    "COMPANY_FINANCIAL_OBSERVATIONS_SCHEMA", "WriteCompanyFinancialObservations", "ReadCompanyFinancialObservations",
    "ReadRoundCompanyFinancialObservations", "ReadCommittedCompanyFinancialReports",
]
