"""One relational codec for source financial identities in collection and publication."""

from collections.abc import Sequence
from datetime import date, datetime
from sqlite3 import Row

from trader.download.domain.company_evidence import CompanyFinancial, CompanyInputValidationFailure
from trader.download.domain.company_source_records import (
    CompanyFinancialBatchCoverage,
    CompanyFinancialField,
    CompanyFinancialMissingField,
    CompanyFinancialSourceObservation,
    CompanyFinancialSourceValues,
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

_BATCH_FIELDS: tuple[tuple[CompanyFinancialField, str], ...] = (
    ("basic_eps", "eps"),
    ("book_value_per_share", "bps"),
    ("revenue_growth_pct", "revenue_growth_pct"),
    ("net_profit_growth_pct", "profit_growth_pct"),
    ("core_profit_growth_pct", "core_profit_growth_pct"),
    ("roe_pct", "roe_pct"),
    ("parent_net_profit", "parent_profit"),
    ("core_net_profit", "core_profit"),
)


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


def ReadRoundLatestCompanyFinancialObservations(
    connection: SqliteConnection, scope: str,
) -> tuple[CompanyFinancialSourceObservation, ...]:
    """Return the exact latest observation per issuer in one completed round."""
    return tuple(_decode(row) for row in connection.run(
        "WITH latest AS (SELECT code, MAX(report_date) AS report_date FROM company_round_member "
        "WHERE scope=? GROUP BY code) "
        "SELECT o.* FROM latest l JOIN company_round_member m ON m.scope=? AND m.code=l.code AND m.report_date=l.report_date "
        "JOIN company_financial_observation o ON o.code=m.code AND o.report_date=m.report_date AND o.record_hash=m.record_hash "
        "ORDER BY m.code",
        (scope, scope),
    ))


def ReadRoundCompanyFinancialBatch(
    connection: SqliteConnection, scope: str, population: int,
) -> CompanyFinancialBatchCoverage:
    """Summarize the frozen pagination and latest observed periods in one read transaction."""
    accepted, first, last, expected, maximum_pages, expected_rows, maximum_rows, raw_rows = connection.run(
        "SELECT COUNT(*), MIN(page), MAX(page), MIN(pages), MAX(pages), MIN(total), MAX(total),"
        " COALESCE(SUM(raw_count), 0) FROM company_round_page WHERE scope=?",
        (scope,),
    ).require_one()
    if accepted and (
        first != 1 or last != accepted or expected != maximum_pages or expected_rows != maximum_rows
        or accepted > expected or raw_rows > expected_rows or accepted == expected and raw_rows != expected_rows
    ):
        raise CompanyInputValidationFailure("company_financial_batch_pagination_inconsistent")

    present = ", ".join(f"COUNT(o.{column})" for _, column in _BATCH_FIELDS)
    all_present = " AND ".join(f"o.{column} IS NOT NULL" for _, column in _BATCH_FIELDS)
    observed, matched, complete, profit_pair, *counts = connection.run(
        f"""WITH latest AS (
            SELECT code, MAX(report_date) AS report_date FROM company_round_member
            WHERE scope=? AND record_hash IS NOT NULL GROUP BY code
        )
        SELECT COUNT(*), COUNT(o.code),
            COUNT(CASE WHEN {all_present} THEN 1 END),
            COUNT(CASE WHEN o.parent_profit IS NOT NULL AND o.core_profit IS NOT NULL THEN 1 END),
            {present}
        FROM latest l JOIN company_round_member m ON m.scope=? AND m.code=l.code AND m.report_date=l.report_date
        LEFT JOIN company_financial_observation o ON o.code=m.code AND o.report_date=m.report_date AND o.record_hash=m.record_hash""",
        (scope, scope),
    ).require_one()
    if observed != matched or observed > population or observed > raw_rows:
        raise CompanyInputValidationFailure("company_financial_batch_membership_inconsistent")

    return CompanyFinancialBatchCoverage(
        population=population,
        accepted_pages=accepted,
        expected_pages=expected,
        accepted_raw_rows=raw_rows,
        expected_raw_rows=expected_rows,
        latest_observed_stocks=observed,
        latest_complete_stocks=complete,
        latest_profit_pair_stocks=profit_pair,
        missing_fields=tuple((field, observed - count) for (field, _), count in zip(_BATCH_FIELDS, counts, strict=True)),
    )


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
    "ReadRoundCompanyFinancialObservations", "ReadRoundLatestCompanyFinancialObservations", "ReadRoundCompanyFinancialBatch",
    "ReadCommittedCompanyFinancialReports",
]
