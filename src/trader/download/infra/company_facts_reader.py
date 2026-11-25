"""Read the compact company publication without loading source bodies."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import VerifiedSqlitePublication, VerifySqliteFile

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from trader.download.application.company_round_progress import CompanyEventFieldCoverage, CompanyFieldOutcomeCounts, CompanyFormalCoverage
from trader.download.domain.company_document_links import CompanyDocumentLinkSnapshot
from trader.download.domain.company_events import CompanyDataState, CompanyEventPolicy, RepurchasePurpose
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_field_results import CompanyFieldName, CompanyFieldSnapshot
from trader.download.domain.company_source_records import CompanyFinancialField
from trader.download.infra.company_document_link_rows import ReadCompanyDocumentLinks
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_facts_protocol import (
    DATA_STATES, REPURCHASE_PURPOSES, CompanyExtractionHash, CompanyInteger, EncodeCompanyDate, EncodeCompanyTime,
)
from trader.download.infra.company_facts_schema import COMPANY_FACTS_SCHEMA
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.infra.sqlite_connection import (
    SqliteConnection,
    EnsureSqliteSchema,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)

_FINANCIAL_FIELDS: tuple[CompanyFinancialField, ...] = (
    "basic_eps", "book_value_per_share", "revenue_growth_pct", "net_profit_growth_pct",
    "core_profit_growth_pct", "roe_pct", "parent_net_profit", "core_net_profit",
)


class SQLiteCompanyEvidenceReader:
    """Only the published, indexed projection is visible to recommendation."""

    def __init__(
        self, path: Path, *, event_policy: CompanyEventPolicy, sqlite_policy: SqliteIoPolicy,
        io_budget: CompanySyncIoBudget | None = None,
    ) -> None:
        self._sqlite_policy = sqlite_policy
        self._io_budget = io_budget
        self.path = path
        self._extraction_hash = CompanyExtractionHash(event_policy)
        self._unlock_days = event_policy.unlock_days
        self._file_verification: VerifiedSqlitePublication | None = None

    @contextmanager
    def _session(self, *, cancel_requested: Callable[[], bool] | None = None) -> Iterator[SqliteConnection]:
        if self._io_budget is not None:
            with self._io_budget.session(self.path, SqliteConnectionOptions(read_only=True),
                sqlite_policy=self._sqlite_policy, cancel_requested=cancel_requested) as connection:
                yield connection
        else:
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True),
                sqlite_policy=self._sqlite_policy, cancel_requested=cancel_requested) as connection:
                yield connection

    def _publication_version(self, connection: SqliteConnection, *, check: Callable[[], None] | None = None) -> str:
        if check is not None:
            check()
        EnsureSqliteSchema(connection, COMPANY_FACTS_SCHEMA, exact=True)
        identity = ReadCompanyPublicationIdentity(connection)
        if check is not None:
            check()
        if identity is None:
            return ""
        self._file_verification = VerifySqliteFile(
            self.path,
            sqlite_policy=self._sqlite_policy,
            previous=self._file_verification,
            check=check,
        )
        if identity.extraction_hash != self._extraction_hash:
            raise ValueError("company_publication_identity_mismatch")
        return identity.content_hash.hex()

    def read_document_links(
        self, codes: Sequence[str], *, cancel_requested: Callable[[], bool] | None = None,
    ) -> CompanyDocumentLinkSnapshot:
        """Preload links from the same formal owner; never inspect collection state."""
        def check() -> None:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_document_link_read_cancelled")

        check()
        if not self.path.exists():
            return CompanyDocumentLinkSnapshot("", ())
        try:
            with self._session(cancel_requested=cancel_requested) as connection:
                with SqliteTransaction(connection):
                    version = self._publication_version(connection, check=check)
                    links = ReadCompanyDocumentLinks(connection, codes) if version else ()
                    check()
                    return CompanyDocumentLinkSnapshot(version, links)
        except sqlite3.Error as exc:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_document_link_read_cancelled") from exc
            raise RuntimeError("company_evidence_links_unavailable") from exc

    def read(self, codes: Sequence[str]) -> tuple[CompanyFacts, ...]:
        snapshots: list[CompanyFacts] = []
        self.visit(codes, snapshots.append)
        return tuple(snapshots)

    def read_fields(
        self, codes: Sequence[str], *, cancel_requested: Callable[[], bool] | None = None,
    ) -> CompanyFieldSnapshot:
        """Return only published field conclusions with their pinned fact identity."""
        def check_cancelled() -> None:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_field_read_cancelled")

        check_cancelled()
        if not self.path.exists():
            return CompanyFieldSnapshot("", ())
        try:
            with self._session(cancel_requested=cancel_requested) as connection:
                with SqliteTransaction(connection):
                    version = self._publication_version(connection, check=check_cancelled)
                    results = ReadCompanyFieldResults(connection, codes) if version else ()
                    if results:
                        self._require_current_field_rules(connection)
                    check_cancelled()
                    return CompanyFieldSnapshot(version, results)
        except sqlite3.Error as exc:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_field_read_cancelled") from exc
            raise RuntimeError("company_fields_unreadable") from exc

    @staticmethod
    def _require_current_field_rules(connection: SqliteConnection) -> None:
        marker = connection.run("SELECT rules_hash FROM company_field_proof_rule WHERE singleton=1").fetchone()
        if marker is None or marker[0] != CompanyFieldProofRulesHash():
            raise ValueError("company_field_rules_unreproved")

    def read_field_counts(
        self,
        *,
        codes: Sequence[str] = (),
        as_of: datetime | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> CompanyFieldOutcomeCounts | None:
        """Count conclusions and pending documents from the same formal snapshot."""
        def check_cancelled() -> None:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_field_read_cancelled")

        check_cancelled()
        if not self.path.exists():
            return None
        try:
            with self._session(cancel_requested=cancel_requested) as connection, SqliteTransaction(connection):
                version = self._publication_version(connection, check=check_cancelled)
                if not version:
                    return None
                total, verified, present, pending, undisclosed, inapplicable, documents = connection.run("""
                    SELECT COUNT(*),
                      COUNT(CASE WHEN state='verified' THEN 1 END),
                      COUNT(CASE WHEN state='present_unverified' THEN 1 END),
                      COUNT(CASE WHEN applicable=1 AND state NOT IN ('verified', 'present_unverified', 'not_disclosed') THEN 1 END),
                      COUNT(CASE WHEN state='not_disclosed' THEN 1 END),
                      COUNT(CASE WHEN applicable=0 THEN 1 END),
                      (SELECT COALESCE(SUM(pending_docs), 0) FROM company_facts)
                    FROM company_field_result
                    """).require_one()
                if total:
                    self._require_current_field_rules(connection)
                coverage = self._coverage(connection, codes, as_of, check_cancelled) if as_of is not None else None
                check_cancelled()
                return CompanyFieldOutcomeCounts(
                    version,
                    CompanyInteger(verified),
                    CompanyInteger(present),
                    CompanyInteger(pending),
                    CompanyInteger(undisclosed),
                    CompanyInteger(inapplicable),
                    CompanyInteger(documents),
                    coverage,
                )
        except sqlite3.Error as exc:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_field_read_cancelled") from exc
            raise RuntimeError("company_fields_unreadable") from exc

    def _coverage(
        self, connection: SqliteConnection, codes: Sequence[str], as_of: datetime, check: Callable[[], None],
    ) -> CompanyFormalCoverage:
        requested = tuple(sorted(set(codes)))
        if as_of.utcoffset() is None or len(requested) > 12_000:
            raise ValueError("company_coverage_scope_invalid")
        complete, partial = (DATA_STATES[state] for state in (CompanyDataState.COMPLETE, CompanyDataState.PARTIAL))
        target_day = EncodeCompanyDate(as_of.date())
        horizon = EncodeCompanyDate(as_of.date() + timedelta(days=self._unlock_days))
        cutoff = EncodeCompanyTime(as_of)
        totals = [0] * (9 + len(_FINANCIAL_FIELDS))
        other_totals: dict[CompanyFieldName, list[int]] = {}
        reduction_totals: dict[CompanyFieldName, list[int]] = {}
        for offset in range(0, len(requested), 512):
            check()
            batch = requested[offset : offset + 512]
            slots = ",".join("?" for _ in batch)
            row = connection.run(f"""
                WITH scoped AS (
                    SELECT f.finance_state=? AS finance_complete,
                      f.disclosed_at<=? AND f.report_date<=? AS scoring_report,
                      l.disclosed_at<=? AND l.report_date<=? AS latest_report,
                      f.eps, f.bps, f.revenue_growth_pct, f.profit_growth_pct, f.core_profit_growth_pct, f.roe_pct,
                      f.parent_profit AS scoring_parent_profit, f.core_profit AS scoring_core_profit,
                      l.parent_profit AS latest_parent_profit, l.core_profit AS latest_core_profit,
                      f.pledge_state=? AND f.pledge_date<=? AS pledge, f.pledge_pct,
                      f.unlock_state=? AND f.unlock_checked_at<=? AND f.unlock_end_date>=? AS unlock,
                      EXISTS(SELECT 1 FROM company_unlock u WHERE u.code=f.code
                        AND u.unlock_date BETWEEN ? AND ?) AS scheduled,
                      f.announcement_state=? AS announcement_complete, f.announcement_state=? AS announcement_partial,
                      EXISTS(SELECT 1 FROM company_risk_event r WHERE r.code=f.code AND r.first_disclosed_at<=?) AS risk
                    FROM company_facts f LEFT JOIN company_latest_financial l ON l.code=f.code
                    WHERE f.code IN ({slots})
                ), visible AS (
                    SELECT scoped.*,
                      CASE WHEN latest_report THEN latest_parent_profit WHEN scoring_report THEN scoring_parent_profit END AS parent_profit,
                      CASE WHEN latest_report THEN latest_core_profit WHEN scoring_report THEN scoring_core_profit END AS core_profit
                    FROM scoped
                )
                SELECT COUNT(CASE WHEN finance_complete AND scoring_report THEN 1 END),
                  COUNT(CASE WHEN parent_profit IS NOT NULL AND core_profit IS NOT NULL THEN 1 END),
                  COUNT(CASE WHEN pledge AND pledge_pct>0 THEN 1 END),
                  COUNT(CASE WHEN pledge AND pledge_pct=0 THEN 1 END),
                  COUNT(CASE WHEN unlock AND scheduled THEN 1 END),
                  COUNT(CASE WHEN unlock AND NOT scheduled THEN 1 END),
                  COUNT(CASE WHEN announcement_complete THEN 1 END),
                  COUNT(CASE WHEN announcement_partial THEN 1 END),
                  COUNT(CASE WHEN risk THEN 1 END),
                  COUNT(CASE WHEN scoring_report THEN eps END), COUNT(CASE WHEN scoring_report THEN bps END),
                  COUNT(CASE WHEN scoring_report THEN revenue_growth_pct END),
                  COUNT(CASE WHEN scoring_report THEN profit_growth_pct END),
                  COUNT(CASE WHEN scoring_report THEN core_profit_growth_pct END),
                  COUNT(CASE WHEN scoring_report THEN roe_pct END), COUNT(parent_profit), COUNT(core_profit)
                FROM visible
                """, (
                    complete, cutoff, target_day, cutoff, target_day, complete, target_day,
                    complete, cutoff, horizon, target_day, horizon, complete, partial, cutoff, *batch,
                )).require_one()
            for index, value in enumerate(row):
                totals[index] += CompanyInteger(value)

            for field_row in connection.run(f"""
                WITH event_fields AS (
                    SELECT code, event_key, field, state, applicable,
                      field IN ('start_date','end_date','planned_pct','completion_disclosed_at') AS reduction
                    FROM company_field_result WHERE code IN ({slots})
                )
                SELECT e.reduction, e.field,
                  COUNT(CASE WHEN e.state='verified' THEN 1 END),
                  COUNT(CASE WHEN e.state='present_unverified' THEN 1 END),
                  COUNT(CASE WHEN e.applicable=1 AND e.state NOT IN ('verified','present_unverified','not_disclosed') THEN 1 END),
                  COUNT(CASE WHEN e.state='not_disclosed' THEN 1 END),
                  COUNT(CASE WHEN e.applicable=0 THEN 1 END),
                  COUNT(CASE WHEN e.applicable=1 AND (CASE e.field
                    WHEN 'start_date' THEN plan.start_date
                    WHEN 'end_date' THEN plan.end_date
                    WHEN 'planned_pct' THEN plan.planned_pct
                    WHEN 'completion_disclosed_at' THEN plan.completion_disclosed_at
                    WHEN 'actual_pct' THEN p.actual_pct
                    WHEN 'repurchase_purpose' THEN NULLIF(p.repurchase_purpose, ?)
                    WHEN 'contract_amount' THEN p.contract_amount
                    WHEN 'annual_revenue' THEN p.annual_revenue
                    WHEN 'revenue_report_date' THEN p.revenue_report_date
                    WHEN 'revenue_disclosed_at' THEN p.revenue_disclosed_at
                    WHEN 'contract_revenue_pct' THEN p.contract_revenue_pct
                    WHEN 'amount_basis' THEN NULLIF(p.amount_basis, 0)
                  END) IS NULL THEN 1 END)
                FROM event_fields e
                LEFT JOIN company_risk_event r ON e.reduction AND r.code=e.code AND r.event_key=e.event_key
                LEFT JOIN company_reduction_plan plan ON plan.risk_id=r.id
                LEFT JOIN company_positive_event p ON NOT e.reduction AND p.code=e.code AND p.event_key=e.event_key
                WHERE COALESCE(r.last_disclosed_at,p.last_disclosed_at)<=?
                GROUP BY e.reduction, e.field
                """, (*batch, REPURCHASE_PURPOSES[RepurchasePurpose.UNKNOWN], cutoff)):
                totals_by_field = reduction_totals if CompanyInteger(field_row[0]) else other_totals
                # The validated schema restricts this column to CompanyFieldName.
                field = cast(CompanyFieldName, field_row[1])
                field_totals = totals_by_field.setdefault(field, [0] * 6)
                for index, value in enumerate(field_row[2:]):
                    field_totals[index] += CompanyInteger(value)

        (financial_complete, latest_profit_values, pledge_with_value, pledge_without_event,
         unlock_with_event, unlock_without_event, announcement_complete, announcement_partial,
         verified_risk_stocks, *financial_counts) = totals
        return CompanyFormalCoverage(
            stocks=len(requested),
            financial_complete=financial_complete,
            latest_profit_values=latest_profit_values,
            pledge_with_value=pledge_with_value,
            pledge_without_event=pledge_without_event,
            unlock_with_event=unlock_with_event,
            unlock_without_event=unlock_without_event,
            announcement_complete=announcement_complete,
            announcement_partial=announcement_partial,
            verified_risk_stocks=verified_risk_stocks,
            financial_fields=tuple(zip(_FINANCIAL_FIELDS, financial_counts, strict=True)),
            other_field_counts=tuple(
                (field, CompanyEventFieldCoverage(*counts)) for field, counts in sorted(other_totals.items())
            ),
            reduction_field_counts=tuple(
                (field, CompanyEventFieldCoverage(*counts)) for field, counts in sorted(reduction_totals.items())
            ),
        )

    def publication_identity(self, *, cancel_requested: Callable[[], bool] | None = None) -> str:
        """Observe the committed identity without verifying or loading the fact payload."""
        if cancel_requested is not None and cancel_requested():
            raise InterruptedError("company_observation_cancelled")
        if not self.path.exists():
            return ""
        try:
            with self._session(cancel_requested=cancel_requested) as connection:
                EnsureSqliteSchema(connection, COMPANY_FACTS_SCHEMA, exact=True)
                identity = ReadCompanyPublicationIdentity(connection)
                if cancel_requested is not None and cancel_requested():
                    raise InterruptedError("company_observation_cancelled")
                if identity is None:
                    return ""
                if identity.extraction_hash != self._extraction_hash:
                    raise ValueError("company_publication_identity_mismatch")
                return identity.content_hash.hex()
        except sqlite3.Error as exc:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_observation_cancelled") from exc
            raise RuntimeError("company_publication_unreadable") from exc

    def publication_version(self) -> str:
        if not self.path.exists():
            return ""
        try:
            with self._session() as connection:
                return self._publication_version(connection)
        except sqlite3.Error as exc:
            raise RuntimeError("company_snapshot_unreadable") from exc

    def visit(
        self,
        codes: Sequence[str],
        consume: Callable[[CompanyFacts], None],
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        def check_cancelled() -> None:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_preload_cancelled")

        check_cancelled()
        if not self.path.exists():
            return
        captured: list[CompanyFacts] = []
        try:
            with self._session(cancel_requested=cancel_requested) as connection:
                with SqliteTransaction(connection):
                    if not self._publication_version(connection, check=check_cancelled):
                        return
                    for offset in range(0, len(codes), 512):
                        check_cancelled()
                        batch = tuple(codes[offset : offset + 512])
                        captured.extend(ReadCompanyFactRows(connection, batch))
                    check_cancelled()
        except sqlite3.Error as exc:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_preload_cancelled") from exc
            raise RuntimeError("company_snapshot_unreadable") from exc
        check_cancelled()
        self._consume_publication(codes, captured, consume, cancel_requested)

    @staticmethod
    def _consume_publication(
        codes: Sequence[str],
        captured: list[CompanyFacts],
        consume: Callable[[CompanyFacts], None],
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        # Invoke consumers only after releasing the pinned SQLite snapshot.
        by_code = {row.code: row for row in captured}
        for code in codes:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_preload_cancelled")
            if code not in by_code:
                continue
            consume(by_code[code])
