"""Immutable first-pass field decisions and their collection acknowledgement."""

from collections.abc import Sequence
from dataclasses import dataclass

from trader.download.infra.company_facts_index import CompanyInputReceipt
from trader.infra.sqlite_connection import SqliteConnection


@dataclass(frozen=True, slots=True)
class CompanyFieldAttempt:
    disclosure_id: int
    code: str
    record_hash: str
    event_id: str
    field: str
    rule_id: str
    receipt: CompanyInputReceipt


def WriteCompanyFieldAttempts(connection: SqliteConnection, attempts: Sequence[CompanyFieldAttempt]) -> None:
    connection.run_many(
        "INSERT OR IGNORE INTO company_field_repairs VALUES(?,?,?,?,?,?,'pending')",
        ((item.disclosure_id, item.code, item.record_hash, item.event_id, item.field, item.rule_id) for item in attempts),
    )


__all__ = ["CompanyFieldAttempt", "WriteCompanyFieldAttempts"]
