"""Admit both current company databases before initializing either writer."""

from collections.abc import Callable
from pathlib import Path
import sqlite3

from trader.download.domain.company_evidence import CompanyDatabaseIncompatibleError
from trader.download.infra.company_disclosure_queries import ActivateDisclosureQueryPolicy
from trader.download.infra.company_evidence_sql import SCHEMA
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity, SQLiteCompanyFactsIndex
from trader.download.infra.company_facts_schema import COMPANY_FACTS_SCHEMA
from trader.download.infra.company_pipeline_queries import PrepareCompanyPipelineQueries
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema, HasSqliteSchema, SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction,
)
from trader.infra.sqlite_policy import SqliteIoPolicy


def _validate_contract(connection: SqliteConnection) -> None:
    if [row[0] for row in connection.run("SELECT schema_id FROM contract")] != ["company_collection"]:
        raise CompanyDatabaseIncompatibleError("company_evidence_database_contract_mismatch")


def _initialize_contract(connection: SqliteConnection) -> None:
    connection.run("INSERT INTO contract VALUES ('company_collection')")


def InitializeCompanyEvidence(
    path: Path, facts: SQLiteCompanyFactsIndex, *, sqlite_policy: SqliteIoPolicy,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    """Reject incompatible downloads without changing the paired databases."""
    if path.exists() and not facts.path.exists():
        raise ValueError("company_database_pair_incomplete")
    try:
        if path.exists():
            try:
                with SqliteSession(
                    path, SqliteConnectionOptions(read_only=True), cancel_requested=cancel_requested,
                    sqlite_policy=sqlite_policy,
                ) as connection:
                    HasSqliteSchema(connection, SCHEMA, validate=_validate_contract)
            except sqlite3.DatabaseError as exc:
                if str(exc) != "sqlite_schema_unsupported":
                    raise
                raise CompanyDatabaseIncompatibleError("company_evidence_database_contract_mismatch") from exc
        if facts.path.exists():
            try:
                with SqliteSession(
                    facts.path, SqliteConnectionOptions(read_only=True), cancel_requested=cancel_requested,
                    sqlite_policy=sqlite_policy,
                ) as formal:
                    if HasSqliteSchema(formal, COMPANY_FACTS_SCHEMA):
                        ReadCompanyPublicationIdentity(formal)
                        # Reference preparation may create an exclusions-only store.
                        if not path.exists() and formal.run(
                            "SELECT 1 FROM company_facts UNION ALL SELECT 1 FROM company_input_receipt LIMIT 1"
                        ).fetchone():
                            raise ValueError("company_database_pair_incomplete")
            except sqlite3.DatabaseError as exc:
                if str(exc) != "sqlite_schema_unsupported":
                    raise
                raise CompanyDatabaseIncompatibleError("company_facts_database_contract_mismatch") from exc
        facts.initialize()
        path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteSession(
            path, SqliteConnectionOptions(journal_mode="WAL", foreign_keys=True),
            cancel_requested=cancel_requested, sqlite_policy=sqlite_policy,
        ) as connection:
            connection.run("PRAGMA auto_vacuum=INCREMENTAL")
            EnsureSqliteSchema(
                connection, SCHEMA, allow_create=True, initialize=_initialize_contract, validate=_validate_contract,
            )
            with SqliteTransaction(connection, immediate=True):
                ActivateDisclosureQueryPolicy(connection)
                PrepareCompanyPipelineQueries(connection)
    except sqlite3.Error as exc:
        if str(exc) == "interrupted" and cancel_requested is not None and cancel_requested():
            raise InterruptedError("company_sync_cancelled") from exc
        raise RuntimeError("company_evidence_persistence_unavailable") from exc


__all__ = ["InitializeCompanyEvidence"]
