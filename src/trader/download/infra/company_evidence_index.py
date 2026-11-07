"""Single-writer incremental evidence index and transactional published read model."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import sqlite3
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

from trader.download.domain.company_events import CompanyDataState, CompanyEventPolicy
from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
from trader.download.infra.company_announcement_links import (
    EnsureCompanyAnnouncementLinks, LinkCompanyAnnouncementNumbers, WriteCompanyAnnouncementNumbers,
)
from trader.download.domain.company_event_extraction import (
    CompanyDocumentNeedsReview, ExtractCompanyLinkCandidate, ResolveCompanyLinks,
)
from trader.download.domain.company_evidence import (
    CompanyDocumentExtraction,
    CompanyDocument,
    IsTransientCompanyDocumentFailure,
    CompanyDocumentPolicies,
    CompanyDocumentTask,
    CompanyDocumentWorkload,
    CompanyOcrDiagnostic,
    CompanyListing,
    CompanyStockProgress,
    Disclosure,
    DisclosureCoverageMismatch,
    DisclosureMonths,
    DisclosurePage,
    DisclosurePartition,
    DisclosurePartitionRetry,
    DisclosureQuery,
    DisclosureSecurity,
    DocumentRetry,
    DocumentRepairMode,
    EvidenceMarket,
    Market,
    StructuredCompanyPage,
    StructuredCompanyQuery,
)
from trader.download.domain.company_facts import CompanyFacts, CompileCompanyFacts
from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.infra.company_field_publication import PrepareCompanyFieldResults
from trader.download.infra.company_disclosure_queries import (
    ActivateDisclosureQueryPolicy,
    CommitDisclosureQueryPage,
    DeferDisclosurePartition,
    DisclosureQueryKey,
    ReadDisclosureQueries,
    ReconcileDisclosureQueries,
    RestartDisclosureQuery,
    SeedDisclosureQueries,
    ValidateDisclosureQueryPage,
)
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.infra.company_link_codec import EncodeCompanyLink
from trader.download.infra.company_supersession import PublishCompanySupersessions
from trader.download.infra.company_coverage_progress import ReadCompanyCoverageProgress
from trader.download.infra.download_failure_diagnostics import FAILURE_DIAGNOSTICS_SQL, WriteDownloadFailure
from trader.download.domain.download_failure import DownloadFailureConfiguration, DownloadFailureRecord
from trader.download.infra.company_evidence_sql import (
    DISCLOSURE_QUERY_SCHEMA,
    DOCUMENT_EXTRACTION_SCHEMA,
    DOCUMENT_RETRY_SCHEMA,
    SCHEMA,
)
from trader.download.infra.company_fact_inputs import (
    BindCompanyEvidenceSource,
    BuildCompanySnapshot,
    DecodeCompanyDisclosure,
    ReadCompanySnapshotRows,
    ReadCompanySnapshotShared,
)
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_field_recovery import (
    CompleteCompanyFieldRepairs,
    CompanyFieldRepairReceipts,
    EnsureCompanyFieldRepairs,
    QueueCompanyFieldRepairs,
    QueueCompanyRepurchaseRechecks,
    QueueCompanyReductionRechecks,
    PrioritizeCompanyRepurchaseInputs,
    RegisterCompanyFieldAttempts,
    RegisterCompanyMaterialDependencies,
    RestoreCompanyMaterialDependencies,
    RestoreCompanyFieldRepairs,
)
from trader.download.infra.company_material_cleanup import ReleaseCompanyMaterialFiles, ReleasePublishedCompanyMaterial
from trader.download.infra.company_publication_reads import PendingCompanyContractLinkCodes, ReadCompanyPublicationLinks
from trader.download.infra.company_contract_link_rules import (
    ApplyCompanyContractLinkRefreshes, CompanyContractLinkRefresh, EnsureCompanyContractLinkRules,
    WriteCompanyContractLinkRules,
)
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity, SQLiteCompanyFactsIndex
from trader.download.domain.company_annual_income import ApplyCompanyAnnualIncome, CompanyAnnualIncome, CompanyAnnualIncomeRequest
from trader.download.infra.company_annual_income_index import (
    EnsureCompanyAnnualIncome,
    PlanCompanyAnnualIncomeRequests,
    WriteCompanyAnnualIncome,
)
from trader.download.infra.company_facts_protocol import EVIDENCE_SOURCES, DecodeCompanyLabel, RequireCompanyTime
from trader.download.infra.company_pipeline_queries import (
    CheckDisclosureQueryContract,
    CountCompanyUnpublishedBodies,
    EnsureCompanyPipelineQueries,
    IndexCompanyDocument,
    MarkCompanyDirty,
    MarkCompanyMarketDirty,
    PrepareCompanyPublication,
    ReleaseCompletedCompanyQueryRows,
    SelectCompanyDocuments,
)
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)

class SQLiteCompanyEvidenceIndex:
    """Own writes to the fresh database; all connections have bounded lifetimes."""

    def __init__(
        self,
        path: Path,
        *,
        facts_path: Path,
        documents: CompanyDocumentFiles,
        cancel_requested: Callable[[], bool] | None = None,
        event_policy: CompanyEventPolicy,
        stock_progress: Callable[[CompanyStockProgress], None] | None = None,
        listings: tuple[CompanyListing, ...] = (), sqlite_policy: SqliteIoPolicy,
        raw_files: CompanyRawFiles | None = None,
        batch_publication_reads: bool = False,
    ) -> None:
        self._sqlite_policy = sqlite_policy
        self.path = path
        if path.resolve() == facts_path.resolve():
            raise ValueError("company_paths_must_be_separate")
        self.facts_path = facts_path
        self._documents = documents
        self._raw_files = raw_files
        self._batch_publication_reads = batch_publication_reads
        self._facts = SQLiteCompanyFactsIndex(facts_path, event_policy=event_policy, cancel_requested=cancel_requested, sqlite_policy=self._sqlite_policy)
        self._cancel_requested = cancel_requested
        self._event_policy = event_policy
        self._stock_progress = stock_progress
        if len({item.code for item in listings}) != len(listings):
            raise ValueError("company_listing_identity_invalid")
        self._listings = listings
        self._run_cutoff: datetime | None = None

    def _check_cancelled(self) -> None:
        if self._cancel_requested is not None and self._cancel_requested():
            raise InterruptedError("company_sync_cancelled")

    def publish_file(self) -> None:
        self._facts.publish_file()

    @contextmanager
    def _connect(self, *, accepted_document: bool = False) -> Iterator[SqliteConnection]:
        if not accepted_document:
            self._check_cancelled()
        try:
            with SqliteSession(
                self.path,
                SqliteConnectionOptions(foreign_keys=True),
                cancel_requested=None if accepted_document else self._cancel_requested, sqlite_policy=self._sqlite_policy,
            ) as connection:
                yield connection
        except sqlite3.Error as exc:
            if str(exc) == "interrupted" and self._cancel_requested is not None and self._cancel_requested():
                raise InterruptedError("company_sync_cancelled") from exc
            raise RuntimeError("company_evidence_persistence_unavailable") from exc

    def initialize(self) -> None:
        self._check_cancelled()
        if self.path.exists() and not self.facts_path.exists():
            raise ValueError("company_database_pair_incomplete")
        if self.facts_path.exists() and not self.path.exists():
            # Public reference preparation may have created an exclusions-only
            # formal store before the first company collection.
            with SqliteSession(self.facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as formal:
                ReadCompanyPublicationIdentity(formal)
                if formal.run("SELECT 1 FROM company_facts UNION ALL SELECT 1 FROM company_input_receipt LIMIT 1").fetchone():
                    raise ValueError("company_database_pair_incomplete")
        self._facts.initialize()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.path.exists():
                with SqliteSession(
                    self.path, SqliteConnectionOptions(read_only=True), cancel_requested=self._cancel_requested, sqlite_policy=self._sqlite_policy
                ) as connection:
                    if connection.run("SELECT 1 FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT 1").fetchone():
                        self._validate_contract(connection)
                        connection.run("SELECT payload_hash,ocr_diagnostic,ocr_policy_id,ocr_timeout_attempts FROM document_retries LIMIT 0")
            with SqliteSession(
                self.path,
                SqliteConnectionOptions(journal_mode="WAL", foreign_keys=True),
                cancel_requested=self._cancel_requested, sqlite_policy=self._sqlite_policy,
            ) as connection:
                connection.run("PRAGMA auto_vacuum=INCREMENTAL")
                EnsureSqliteSchema(
                    connection,
                    SCHEMA,
                    allow_create=True,
                    initialize=self._initialize_contract,
                    validate=self._validate_contract,
                )
                with SqliteTransaction(connection, immediate=True):
                    connection.run(DOCUMENT_RETRY_SCHEMA)
                    connection.run(DOCUMENT_EXTRACTION_SCHEMA)
                    EnsureCompanyAnnouncementLinks(connection)
                    for statement in DISCLOSURE_QUERY_SCHEMA:
                        connection.run(statement)
                    ActivateDisclosureQueryPolicy(connection)
                    EnsureCompanyPipelineQueries(connection)
                    EnsureCompanyFieldRepairs(connection)
                    EnsureCompanyContractLinkRules(connection)
                    EnsureCompanyAnnualIncome(connection)
                    connection.run(FAILURE_DIAGNOSTICS_SQL)
            self._repair_pending_files()
        except sqlite3.Error as exc:
            if str(exc) == "interrupted" and self._cancel_requested is not None and self._cancel_requested():
                raise InterruptedError("company_sync_cancelled") from exc
            raise RuntimeError("company_evidence_persistence_unavailable") from exc

    @staticmethod
    def _initialize_contract(connection: SqliteConnection) -> None:
        connection.run("INSERT INTO contract VALUES ('company_collection')")

    @staticmethod
    def _validate_contract(connection: SqliteConnection) -> None:
        if connection.run("SELECT 1 FROM sqlite_master WHERE type='table' AND name='contract'").fetchone() is None:
            raise ValueError("company_evidence_database_contract_mismatch")
        contract = connection.run("SELECT schema_id FROM contract").fetchall()
        if [row[0] for row in contract] != ["company_collection"]:
            raise ValueError("company_evidence_database_contract_mismatch")

    def _repair_pending_files(self) -> None:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            for row in connection.run("SELECT id,body_hash,record_hash FROM disclosures WHERE body_retained=1").fetchall():
                self._check_cancelled()
                try:
                    text = self._documents.read(row[1])
                except (OSError, ValueError, EOFError):
                    connection.run(
                        "UPDATE disclosures SET body_checked=0,body_retained=0,body_hash=NULL WHERE id=?", (row[0],)
                    )
                    connection.run("DELETE FROM document_extractions WHERE disclosure_id=?", (row[0],))
                    MarkCompanyDirty(
                        connection,
                        tuple(
                            item[0]
                            for item in connection.run(
                                "SELECT code FROM disclosure_codes WHERE disclosure_id=?", (row[0],)
                            )
                        ),
                    )
                else:
                    WriteCompanyAnnouncementNumbers(connection, row[0], row[2], row[1], ExtractCompanyAnnouncementNumbers(text))
        with (
            SqliteSession(self.facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as formal,
            self._connect() as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            RestoreCompanyMaterialDependencies(connection, formal)
        self._release_files()

    def partitions(self, markets: Sequence[Market], observed_at: datetime) -> tuple[DisclosurePartition, ...]:
        today = observed_at.date()
        cutoff = today - timedelta(days=365)
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            pending_before: set[tuple[str, str, str]] = set()
            expired = tuple(connection.run("SELECT market,start_date,end_date FROM partitions WHERE is_complete=0 AND start_date<?", (cutoff.isoformat(),)))
            for row in expired:
                key = tuple(row)
                connection.run("INSERT OR IGNORE INTO company_expired_ranges VALUES(?,?,?,?)", (*key, observed_at.isoformat()))
                for table in ("partitions", "disclosure_queries", "partition_rows", "disclosure_query_rows", "partition_retries", "disclosure_query_contracts"):
                    connection.run(f"DELETE FROM {table} WHERE market=? AND start_date=? AND end_date=?", key)
                if row[2] >= cutoff.isoformat():
                    self._add_partition(connection, cast(Market, row[0]), cutoff, date.fromisoformat(row[2]))
            for market in markets:
                before_planning = connection.total_changes
                pending_before.update(
                    (row["market"], row["start_date"], row["end_date"])
                    for row in connection.run(
                        "SELECT market,start_date,end_date FROM partitions WHERE market=? AND is_complete=0 AND end_date>=?",
                        (market, cutoff.isoformat()),
                    ).fetchall()
                )
                # Subtract every persisted slice, including pending children. A maximum
                # completed date is not proof that earlier discovery has no holes.
                for start, end in DisclosureMonths(cutoff, today - timedelta(days=7)):
                    self._add_partition(connection, market, start, end)
                # Check each recent day separately: fixed pages survive interruption and don't shift at midnight.
                for offset in range(7):
                    day = today - timedelta(days=offset)
                    # Completed wider slices also own their dates; do not create overlapping owners.
                    self._add_partition(connection, market, day, day)
                connection.run(
                    "DELETE FROM disclosure_queries WHERE market=? AND EXISTS(SELECT 1 FROM partitions p "
                    "WHERE p.market=disclosure_queries.market AND p.start_date=disclosure_queries.start_date AND p.end_date=disclosure_queries.end_date "
                    "AND p.is_complete=1 AND p.end_date>=? AND p.checked_at<?)",
                    (market, (today - timedelta(days=6)).isoformat(), today.isoformat()),
                )
                connection.run(
                    "UPDATE partitions SET next_page=1,expected_rows=NULL,received_rows=0,is_complete=0 "
                    "WHERE market=? AND is_complete=1 AND end_date>=? AND checked_at<?",
                    (market, (today - timedelta(days=6)).isoformat(), today.isoformat()),
                )
                connection.run(
                    "DELETE FROM disclosure_queries WHERE market=? AND EXISTS(SELECT 1 FROM partitions p "
                    "WHERE p.market=disclosure_queries.market AND p.start_date=disclosure_queries.start_date AND p.end_date=disclosure_queries.end_date "
                    "AND p.is_complete=1 AND p.end_date=? AND p.checked_at<?)",
                    (market, today.isoformat(), (observed_at - timedelta(minutes=15)).isoformat()),
                )
                connection.run(
                    "UPDATE partitions SET next_page=1,expected_rows=NULL,received_rows=0,is_complete=0 "
                    "WHERE market=? AND is_complete=1 AND end_date=? AND checked_at<?",
                    (market, today.isoformat(), (observed_at - timedelta(minutes=15)).isoformat()),
                )
                if connection.total_changes != before_planning:
                    MarkCompanyMarketDirty(connection, market)
            rows = connection.run(
                "SELECT * FROM partitions WHERE is_complete=0 AND end_date>=? "
                "ORDER BY CASE WHEN next_page>1 THEN 0 ELSE 1 END,end_date DESC,market,start_date",
                (cutoff.isoformat(),),
            ).fetchall()
            rows = sorted(
                rows,
                key=lambda row: (
                    row["end_date"] < (today - timedelta(days=6)).isoformat(),
                    (row["market"], row["start_date"], row["end_date"]) not in pending_before,
                ),
            )
        return tuple(
            DisclosurePartition(
                cast(Market, row["market"]),
                date.fromisoformat(row["start_date"]),
                date.fromisoformat(row["end_date"]),
                row["next_page"],
                row["expected_rows"],
                row["received_rows"],
            )
            for row in rows
            if row["market"] in markets
        )

    def reusable_partitions(self, markets: Sequence[Market], observed_at: datetime) -> int:
        with self._connect() as connection:
            return sum(
                row[1]
                for row in connection.run(
                    "SELECT market,count(*) FROM partitions WHERE is_complete=1 AND end_date>=? GROUP BY market",
                    ((observed_at.date() - timedelta(days=365)).isoformat(),),
                )
                if row[0] in markets
            )

    def _add_partition(self, connection: SqliteConnection, market: str, start: date, end: date) -> None:
        # Seed only dates with no durable owner. Existing child ranges prevent
        # restart/audit from recreating their already covered parent range.
        ranges = connection.run(
            "SELECT start_date,end_date FROM partitions WHERE market=? AND start_date<=? AND end_date>=? ORDER BY start_date,end_date",
            (market, end.isoformat(), start.isoformat()),
        ).fetchall()
        cursor = start
        for row in ranges:
            left, right = date.fromisoformat(row[0]), date.fromisoformat(row[1])
            if left > cursor:
                connection.run(
                    "INSERT OR IGNORE INTO partitions(market,start_date,end_date) VALUES(?,?,?)",
                    (market, cursor.isoformat(), (left - timedelta(days=1)).isoformat()),
                )
            cursor = max(cursor, right + timedelta(days=1))
        if cursor <= end:
            connection.run(
                "INSERT OR IGNORE INTO partitions(market,start_date,end_date) VALUES(?,?,?)",
                (market, cursor.isoformat(), end.isoformat()),
            )

    def split_partition(self, partition: DisclosurePartition, observed_at: datetime) -> tuple[DisclosurePartition, ...]:
        if partition.start == partition.end:
            raise ValueError("cninfo_single_day_page_limit")
        middle = partition.start + (partition.end - partition.start) // 2
        children = (
            replace(partition, end=middle, page=1, expected_rows=None, received_rows=0),
            replace(partition, start=middle + timedelta(days=1), page=1, expected_rows=None, received_rows=0),
        )
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            connection.run("DELETE FROM partitions WHERE market=? AND start_date=? AND end_date=?", _key(partition))
            for item in children:
                self._add_partition(connection, item.market, item.start, item.end)
                connection.run(
                    "UPDATE partitions SET next_page=1,expected_rows=NULL,received_rows=0,is_complete=0 "
                    "WHERE market=? AND start_date>=? AND end_date<=? AND is_complete=1 AND checked_at<?",
                    (*_key(item), observed_at.date().isoformat()),
                )
            connection.run("DELETE FROM partition_rows WHERE market=? AND start_date=? AND end_date=?", _key(partition))
            connection.run("DELETE FROM coverage WHERE market=? AND start_date=? AND end_date=?", _key(partition))
            rows = connection.run(
                "SELECT * FROM partitions WHERE market=? AND start_date>=? AND end_date<=? AND is_complete=0 ORDER BY start_date",
                _key(partition),
            ).fetchall()
        return tuple(
            DisclosurePartition(
                partition.market,
                date.fromisoformat(row["start_date"]),
                date.fromisoformat(row["end_date"]),
                row["next_page"],
                row["expected_rows"],
                row["received_rows"],
            )
            for row in rows
        )

    def commit_page(self, partition: DisclosurePartition, page: DisclosurePage, observed_at: datetime) -> int:
        if partition.page_size != 30 or partition.filter_identity != "all_announcements":
            raise ValueError("company_query_contract_unverified")
        if partition.query is not None:
            try:
                with self._connect() as connection, SqliteTransaction(connection, immediate=True):
                    CheckDisclosureQueryContract(connection, partition)
                    ValidateDisclosureQueryPage(connection, partition, page)
                    changes = self._write_disclosures(connection, page.rows)
                    CommitDisclosureQueryPage(connection, partition, page)
                return changes
            except ValueError:
                self.restart_partition(partition)
                raise
        self._validate_page(partition, page)
        received = partition.received_rows + len(page.rows)
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            CheckDisclosureQueryContract(connection, partition)
            if partition.page == 1:
                connection.run("DELETE FROM partition_rows WHERE market=? AND start_date=? AND end_date=?", _key(partition))
            changes = self._write_disclosures(connection, page.rows)
            for item in page.rows:
                for code in item.codes:
                    connection.run(
                        "INSERT OR IGNORE INTO partition_rows VALUES(?,?,?,?,?)",
                        (*_key(partition), item.identity, code),
                    )
            connection.run(
                "UPDATE partitions SET next_page=?,expected_rows=?,received_rows=?,is_complete=?,checked_at=?,failure='' "
                "WHERE market=? AND start_date=? AND end_date=?",
                (
                    partition.page + 1,
                    page.total,
                    received,
                    int(not page.has_more),
                    observed_at.isoformat(),
                    *_key(partition),
                ),
            )
            connection.run(
                "DELETE FROM partition_retries WHERE market=? AND start_date=? AND end_date=? AND scope=?",
                DisclosureQueryKey(partition),
            )
            # Removed/corrected listings are recognized only after a complete date slice, never after a failed page.
            if not page.has_more:
                self._finish_partition(connection, partition, observed_at)
        return changes

    def _finish_partition(
        self, connection: SqliteConnection, partition: DisclosurePartition, observed_at: datetime
    ) -> None:
        connection.run("INSERT OR REPLACE INTO coverage VALUES(?,?,?,?)", (*_key(partition), observed_at.isoformat()))
        stale = connection.run(
            "SELECT d.id,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
            "WHERE d.source='cninfo' AND d.kind='announcement' AND d.published_at>=? AND d.published_at<? "
            "AND NOT EXISTS(SELECT 1 FROM partition_rows p WHERE p.market=? AND p.start_date=? AND p.end_date=? "
            "AND p.source_key=d.source_key AND p.code=c.code)",
            (partition.start.isoformat(), (partition.end + timedelta(days=1)).isoformat(), *_key(partition)),
        ).fetchall()
        for row in stale:
            if EvidenceMarket(row["code"]) == partition.market:
                connection.run("DELETE FROM disclosure_codes WHERE disclosure_id=? AND code=?", tuple(row))
                MarkCompanyDirty(connection, (row["code"],))
        # A completed range changes coverage even for issuers with no announcements.
        MarkCompanyMarketDirty(connection, partition.market)
        # Rechecks start a fresh generation; completed page membership has no remaining consumer.
        connection.run("DELETE FROM partition_rows WHERE market=? AND start_date=? AND end_date=?", _key(partition))
        connection.run("DELETE FROM disclosure_query_rows WHERE market=? AND start_date=? AND end_date=?", _key(partition))

    def disclosure_queries(
        self, partition: DisclosurePartition, *, include_completed: bool = False
    ) -> tuple[DisclosurePartition, ...] | None:
        with self._connect() as connection:
            return ReadDisclosureQueries(connection, replace(partition, query=None), include_completed=include_completed)

    def refresh_disclosure_query(self, partition: DisclosurePartition, page: DisclosurePage) -> int | None:
        if partition.query is None or partition.page != 1:
            raise ValueError("company_query_probe_invalid")
        key = DisclosureQueryKey(partition)
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            changed = partition.expected_rows != page.total or any(
                connection.run(
                    "SELECT 1 FROM disclosure_query_rows WHERE market=? AND start_date=? AND end_date=? "
                    "AND scope=? AND record_key=? LIMIT 1",
                    (*key, json.dumps([item.identity, item.codes], separators=(",", ":"))),
                ).fetchone() is None for item in page.rows
            )
            if not changed:
                return None
            fresh = replace(partition, received_rows=0, expected_rows=None)
            ValidateDisclosureQueryPage(connection, fresh, page)
            RestartDisclosureQuery(connection, fresh)
            changes = self._write_disclosures(connection, page.rows)
            CommitDisclosureQueryPage(connection, fresh, page)
            MarkCompanyMarketDirty(connection, partition.market)
        return changes

    def disclosure_members(self, partition: DisclosurePartition) -> tuple[DisclosureSecurity, ...]:
        with self._connect() as connection:
            queries = ReadDisclosureQueries(connection, replace(partition, query=None), include_completed=True)
        return tuple(member for item in queries or () if item.query for member in item.query.securities)

    def split_query(
        self, partition: DisclosurePartition, queries: tuple[DisclosureQuery, ...]
    ) -> tuple[DisclosurePartition, ...]:
        if not queries:
            raise ValueError("cninfo_query_unsplittable")
        if len({query.key for query in queries}) != len(queries) or (
            partition.query is not None and any(query.key == partition.query.key for query in queries)
        ):
            raise ValueError("cninfo_query_split_invalid")
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            SeedDisclosureQueries(connection, partition, queries)
            pending = ReadDisclosureQueries(connection, replace(partition, query=None))
        if pending is None:
            raise RuntimeError("company_disclosure_queries_missing")
        return pending

    def commit_query_coverage(
        self, partition: DisclosurePartition, page: DisclosurePage, observed_at: datetime
    ) -> None:
        # A failed parent check does not invalidate successfully saved child pages.
        # Keep them available for targeted issuer repair or a later verification.
        try:
            with self._connect() as connection, SqliteTransaction(connection, immediate=True):
                ReconcileDisclosureQueries(connection, partition, page)
                connection.run(
                    "UPDATE partitions SET is_complete=1,next_page=1,expected_rows=?,received_rows=?,checked_at=?,failure='' "
                    "WHERE market=? AND start_date=? AND end_date=?",
                    (page.total, page.total, observed_at.isoformat(), *_key(partition)),
                )
                self._finish_partition(connection, partition, observed_at)
                connection.run("DELETE FROM partition_retries WHERE market=? AND start_date=? AND end_date=?", _key(partition))
        except DisclosureCoverageMismatch as exc:
            if exc.refresh_required:
                # A smaller source set or changed sample really invalidates the
                # saved generation. Preserve its historical issuer membership.
                with self._connect() as connection, SqliteTransaction(connection, immediate=True):
                    connection.run(
                        "UPDATE disclosure_queries SET next_page=1,expected_rows=NULL,received_rows=0,is_complete=0 "
                        "WHERE market=? AND start_date=? AND end_date=?",
                        _key(partition),
                    )
                    connection.run(
                        "DELETE FROM disclosure_query_rows WHERE market=? AND start_date=? AND end_date=?", _key(partition)
                    )
            raise

    def partition_retry(self, partition: DisclosurePartition) -> DisclosurePartitionRetry | None:
        with self._connect() as connection:
            row = connection.run(
                "SELECT r.retry_at,CASE WHEN r.scope='' THEN p.failure ELSE q.failure END "
                "FROM partition_retries r JOIN partitions p "
                "ON p.market=r.market AND p.start_date=r.start_date AND p.end_date=r.end_date "
                "LEFT JOIN disclosure_queries q ON q.market=r.market AND q.start_date=r.start_date "
                "AND q.end_date=r.end_date AND q.scope=r.scope "
                "WHERE r.market=? AND r.start_date=? AND r.end_date=? AND r.scope=?",
                DisclosureQueryKey(partition),
            ).fetchone()
        if row is None:
            return None
        if row[1] is None:
            raise ValueError("company_disclosure_retry_invalid")
        return DisclosurePartitionRetry(datetime.fromisoformat(row[0]), row[1])

    def defer_partition(self, partition: DisclosurePartition, retry_after: datetime) -> None:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            DeferDisclosurePartition(connection, partition, retry_after)

    def _validate_page(self, partition: DisclosurePartition, page: DisclosurePage) -> None:
        if partition.expected_rows is not None and partition.expected_rows != page.total:
            self.restart_partition(partition)
            raise ValueError("cninfo_partition_changed")
        received = partition.received_rows + len(page.rows)
        if not page.has_more and received != page.total:
            self.restart_partition(partition)
            raise ValueError("cninfo_page_truncated")
        if page.has_more and received >= page.total:
            self.restart_partition(partition)
            raise ValueError("cninfo_page_count_inconsistent")
        links = tuple((item.identity, code) for item in page.rows for code in item.codes)
        duplicate = len(links) != len(set(links))
        if partition.page > 1:
            with self._connect() as connection:
                duplicate = duplicate or any(
                    connection.run(
                        "SELECT 1 FROM partition_rows WHERE market=? AND start_date=? AND end_date=? AND source_key=? AND code=?",
                        (*_key(partition), identity, code),
                    ).fetchone()
                    is not None
                    for identity, code in links
                )
        if duplicate:
            self.restart_partition(partition)
            raise ValueError("cninfo_duplicate_page_links")

    def restart_partition(self, partition: DisclosurePartition) -> None:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            if partition.query is not None:
                RestartDisclosureQuery(connection, partition)
                return
            connection.run(
                "UPDATE partitions SET next_page=1,expected_rows=NULL,received_rows=0,is_complete=0 "
                "WHERE market=? AND start_date=? AND end_date=?",
                _key(partition),
            )

    def partition_failure(self, partition: DisclosurePartition, failure: str) -> None:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            if partition.query is not None:
                connection.run(
                    "UPDATE disclosure_queries SET failure=? WHERE market=? AND start_date=? AND end_date=? AND scope=?",
                    (failure, *DisclosureQueryKey(partition)),
                )
            connection.run(
                "UPDATE partitions SET failure=? WHERE market=? AND start_date=? AND end_date=?", (failure, *_key(partition))
            )
            MarkCompanyMarketDirty(connection, partition.market)

    def _write_disclosures(self, connection: SqliteConnection, rows: Sequence[Disclosure]) -> int:
        existing: dict[tuple[str, str], str] = {}
        previous_codes: dict[tuple[str, str], set[str]] = {}
        for offset in range(0, len(rows), 128):
            batch = rows[offset:offset + 128]
            placeholders = ",".join("(?,?)" for _ in batch)
            parameters = tuple(value for item in batch for value in (item.source, item.identity))
            for row in connection.run(
                f"WITH requested(source,source_key) AS (VALUES {placeholders}) "
                "SELECT d.source,d.source_key,d.record_hash,c.code FROM requested r "
                "JOIN disclosures d ON d.source=r.source AND d.source_key=r.source_key "
                "LEFT JOIN disclosure_codes c ON c.disclosure_id=d.id",
                parameters,
            ):
                key = (str(row[0]), str(row[1]))
                existing[key] = str(row[2])
                if row[3] is not None:
                    previous_codes.setdefault(key, set()).add(str(row[3]))
        affected = tuple(
            item
            for item in rows
            if existing.get((item.source, item.identity)) != item.digest
        )
        MarkCompanyDirty(connection, tuple({code for item in affected for code in previous_codes.get((item.source, item.identity), ())}))
        changes = connection.run_many(
            "INSERT INTO disclosures(source,source_key,kind,title,published_at,url,record_hash) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(source,source_key) DO UPDATE SET title=excluded.title,published_at=excluded.published_at,"
            "url=excluded.url,record_hash=excluded.record_hash,body_hash=NULL,body_retained=0,body_checked=0 WHERE record_hash<>excluded.record_hash",
            (
                (
                    item.source,
                    item.identity,
                    item.kind,
                    item.title,
                    item.published_at.isoformat(),
                    item.url,
                    item.digest,
                )
                for item in rows
            ),
        ).rowcount
        links = connection.run_many(
            "INSERT OR IGNORE INTO disclosure_codes(code,disclosure_id) "
            "SELECT ?,id FROM disclosures WHERE source=? AND source_key=?",
            ((code, item.source, item.identity) for item in rows for code in item.codes),
        ).rowcount
        connection.run_many(
            "DELETE FROM document_extractions WHERE disclosure_id IN (SELECT id FROM disclosures "
            "WHERE source=? AND source_key=? AND record_hash<>document_extractions.record_hash)",
            ((item.source, item.identity) for item in affected),
        )
        IndexCompanyDocument(connection, affected)
        MarkCompanyDirty(connection, tuple({code for item in (rows if links else affected) for code in item.codes}))
        return changes

    def pending_documents(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        limit: int | None = None,
        policies: CompanyDocumentPolicies,
        excluded: Sequence[tuple[str, str, str]] = (),
        repair: DocumentRepairMode | None = None,
        local_only: bool = False,
    ) -> tuple[CompanyDocumentTask, ...]:
        if limit is not None and not 1 <= limit <= 200:
            raise ValueError("company_document_batch_invalid")
        with self._connect() as connection:
            SelectCompanyDocuments(connection, codes, observed_at, policies, excluded, repair, start=(self._run_cutoff or observed_at).date() - timedelta(days=365), extraction_hash=self._facts.extraction_hash, until=self._run_cutoff)
            with SqliteSession(self.facts_path, SqliteConnectionOptions(read_only=True),
                               sqlite_policy=self._sqlite_policy, cancel_requested=self._cancel_requested) as formal:
                PrioritizeCompanyRepurchaseInputs(connection, formal, codes, observed_at, self._facts.extraction_hash)
            rows = connection.run(
                "SELECT d.source,d.source_key,d.kind,d.title,d.published_at,d.url,d.record_hash,c.code,p.route FROM disclosures d "
                "JOIN disclosure_codes c ON c.disclosure_id=d.id JOIN temp.requested_codes u ON u.code=c.code "
                "JOIN temp.pending_documents p ON p.id=d.id WHERE d.id IN ("
                "SELECT selected.id FROM temp.pending_documents selected JOIN disclosures s ON s.id=selected.id "
                "WHERE (?=0 OR selected.due=1) AND (?=0 OR EXISTS(SELECT 1 FROM document_tasks t "
                "WHERE t.source=s.source AND t.source_key=s.source_key AND t.record_hash=s.record_hash AND t.path IS NOT NULL)) "
                "ORDER BY EXISTS(SELECT 1 FROM temp.company_field_inputs f WHERE f.id=s.id) DESC,"
                "s.published_at DESC,EXISTS(SELECT 1 FROM document_tasks t "
                "WHERE t.source=s.source AND t.source_key=s.source_key AND t.record_hash=s.record_hash AND t.path IS NOT NULL) DESC,"
                "s.id LIMIT ?) "
                "ORDER BY EXISTS(SELECT 1 FROM temp.company_field_inputs f WHERE f.id=d.id) DESC,d.published_at DESC,d.id,c.code",
                (limit is not None or bool(excluded) or repair is not None, local_only, -1 if limit is None else limit),
            ).fetchall()
        items: dict[tuple[str, str], CompanyDocumentTask] = {}
        for row in rows:
            item = DecodeCompanyDisclosure(row)
            key = item.source, item.identity
            previous = items.get(key)
            items[key] = (
                CompanyDocumentTask(item, row[8])
                if previous is None
                else CompanyDocumentTask(replace(previous.disclosure, codes=(*previous.disclosure.codes, *item.codes)), previous.route)
            )
        pending = tuple(items.values())
        return pending[: self._documents.batch_limit(limit)] if pending and limit is not None else pending

    def document_workload(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        policies: CompanyDocumentPolicies,
        *,
        excluded: Sequence[tuple[str, str, str]] = (),
        repair: DocumentRepairMode | None = None,
    ) -> CompanyDocumentWorkload:
        with self._connect() as connection:
            SelectCompanyDocuments(connection, codes, observed_at, policies, excluded, repair, start=(self._run_cutoff or observed_at).date() - timedelta(days=365), extraction_hash=self._facts.extraction_hash, until=self._run_cutoff)
            row = connection.run(
                "SELECT COUNT(*),COALESCE(SUM(due),0),COALESCE(SUM(cooling),0),COALESCE(SUM(ocr),0),"
                "COALESCE(SUM(unextractable),0),MIN(CASE WHEN cooling=1 THEN retry_at END) FROM temp.pending_documents"
            ).require_one()
            local = connection.run(
                "SELECT COUNT(*) FROM temp.pending_documents p JOIN disclosures d ON d.id=p.id "
                "JOIN document_tasks t ON t.source=d.source AND t.source_key=d.source_key AND t.record_hash=d.record_hash "
                "WHERE t.path IS NOT NULL AND p.unextractable=0"
            ).require_one()[0]
            analyzed = CountCompanyUnpublishedBodies(connection, self._run_cutoff or observed_at, self._facts.extraction_hash)
            published = connection.run(
                "SELECT COUNT(DISTINCT p.disclosure_id) FROM document_publications p JOIN disclosures d ON d.id=p.disclosure_id "
                "JOIN temp.requested_codes u ON u.code=p.code WHERE p.record_hash=d.record_hash AND p.extraction_hash=?",
                (self._facts.extraction_hash,),
            ).require_one()[0]
            superseded = connection.run(
                "SELECT COUNT(DISTINCT s.disclosure_id) FROM document_supersessions s JOIN temp.requested_codes u ON u.code=s.code "
                "JOIN disclosures d ON d.id=s.disclosure_id AND d.record_hash=s.record_hash "
                "JOIN disclosures n ON n.id=s.replacement_id AND n.record_hash=s.replacement_hash "
                "JOIN document_publications p ON p.disclosure_id=n.id AND p.code=s.code AND p.record_hash=n.record_hash "
                "AND p.extraction_hash=s.extraction_hash WHERE s.extraction_hash=?", (self._facts.extraction_hash,),
            ).require_one()[0]
            with SqliteSession(self.facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as formal:
                watermarks = ReadCompanyCoverageProgress(connection, formal, self._run_cutoff or observed_at, self._facts.extraction_hash)
            unresolved = connection.run(
                "SELECT COUNT(*) FROM temp.pending_documents p JOIN document_retries r ON r.disclosure_id=p.id "
                "JOIN disclosures d ON d.id=p.id AND d.record_hash=r.record_hash"
            ).require_one()[0]
        return CompanyDocumentWorkload(
            row[0], row[1], row[2], row[3], row[4], datetime.fromisoformat(row[5]) if row[5] else None,
            max(0, row[0] - row[4] - local), local, analyzed, published, superseded,
            self._raw_files.usage() if self._raw_files is not None else 0,
            watermarks,
            unresolved,
        )

    def document_required(self, item: Disclosure) -> bool:
        with self._connect() as connection:
            return connection.run(
                "SELECT 1 FROM disclosures d JOIN company_document_requirements q ON q.disclosure_id=d.id "
                "JOIN disclosure_codes c ON c.disclosure_id=d.id "
                "JOIN company_population u ON u.code=c.code WHERE d.source=? AND d.source_key=? AND d.record_hash=? "
                "AND d.body_checked=0 AND NOT EXISTS(SELECT 1 FROM document_supersessions s "
                "JOIN disclosures n ON n.id=s.replacement_id AND n.record_hash=s.replacement_hash "
                "JOIN document_publications p ON p.disclosure_id=n.id AND p.code=c.code AND p.record_hash=n.record_hash "
                "AND p.extraction_hash=s.extraction_hash WHERE s.disclosure_id=d.id AND s.code=c.code "
                "AND s.record_hash=d.record_hash AND s.extraction_hash=?) LIMIT 1",
                (item.source, item.identity, item.digest, self._facts.extraction_hash),
            ).fetchone() is not None

    def document_stage(self, item: Disclosure, stage: str) -> None:
        local = self._raw_files.local(item) if self._raw_files is not None else None
        with self._connect(accepted_document=True) as connection, SqliteTransaction(connection, immediate=True):
            connection.run(
                "INSERT INTO document_tasks(source,source_key,record_hash,stage,path,payload_hash,size) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(source,source_key,record_hash) DO UPDATE SET stage=excluded.stage,"
                "path=COALESCE(excluded.path,path),payload_hash=COALESCE(excluded.payload_hash,payload_hash),size=COALESCE(excluded.size,size)",
                (item.source, item.identity, item.digest, stage, str(local.path.relative_to(self.path.parent)) if local else None,
                 local.payload_hash if local else None, local.size if local else None),
            )

    def record_failure(self, record: DownloadFailureRecord, *, configuration: DownloadFailureConfiguration) -> None:
        with self._connect(accepted_document=True) as connection, SqliteTransaction(connection, immediate=True):
            WriteDownloadFailure(connection, record, configuration)

    def commit_document(self, item: Disclosure, extraction: CompanyDocumentExtraction, extracted_at: datetime) -> bool:
        # A result already accepted by the main thread is drained on cancellation.
        with self._connect(accepted_document=True) as connection, SqliteTransaction(connection, immediate=True):
            # Reject late or duplicate results before writing text or link candidates.
            existing = connection.run(
                "SELECT id FROM disclosures WHERE source=? AND source_key=? AND record_hash=? AND body_checked=0",
                (item.source, item.identity, item.digest),
            ).fetchone()
            if existing is None:
                return False
            reservation = self._raw_files.reserve(self._documents.root, len(extraction.text.encode("utf-8")) + 4096, analysis=True) if self._raw_files is not None else nullcontext()
            with reservation:
                digest = self._documents.write(extraction.text)
            WriteCompanyAnnouncementNumbers(connection, existing[0], item.digest, digest, ExtractCompanyAnnouncementNumbers(extraction.text))
            candidate = ExtractCompanyLinkCandidate(CompanyDocument(item, extraction.text, True), extracted_at, self._event_policy)
            connection.run(
                "DELETE FROM document_link_candidates WHERE source=? AND source_key=? AND record_hash=? AND extraction_hash=?",
                (item.source, item.identity, item.digest, self._facts.extraction_hash),
            )
            if candidate is not None:
                connection.run("INSERT OR REPLACE INTO document_link_candidates VALUES(?,?,?,?,?)",
                               (item.source, item.identity, item.digest, self._facts.extraction_hash, EncodeCompanyLink(candidate)))
            changed = connection.run(
                "UPDATE disclosures SET body_hash=?,body_checked=1,body_retained=1 WHERE source=? AND source_key=? AND record_hash=? AND body_checked=0",
                (digest, item.source, item.identity, item.digest),
            ).rowcount
            if not changed:
                return False
            WriteCompanyContractLinkRules(connection, item, digest, self._facts.extraction_hash, candidate)
            connection.run(
                "INSERT OR REPLACE INTO document_extractions(disclosure_id,record_hash,payload_hash,method,policy_id,quality,extracted_at) "
                "SELECT id,record_hash,?,?,?,?,? FROM disclosures WHERE source=? AND source_key=? AND record_hash=?",
                (
                    extraction.payload_hash,
                    extraction.method,
                    extraction.policy_id,
                    extraction.quality,
                    extracted_at.isoformat(),
                    item.source,
                    item.identity,
                    item.digest,
                ),
            )
            connection.run(
                "DELETE FROM document_retries WHERE disclosure_id IN "
                "(SELECT id FROM disclosures WHERE source=? AND source_key=? AND record_hash=?)",
                (item.source, item.identity, item.digest),
            )
            MarkCompanyDirty(
                connection,
                tuple(
                    row[0]
                    for row in connection.run(
                        "SELECT c.code FROM disclosure_codes c JOIN disclosures d ON d.id=c.disclosure_id WHERE d.source=? AND d.source_key=?",
                        (item.source, item.identity),
                    )
                ),
            )
        self.document_stage(item, "analyzed")
        return True

    def document_retry(self, item: Disclosure, policy: str) -> DocumentRetry | None:
        with self._connect() as connection:
            row = connection.run(
                "SELECT r.failure,r.retry_at FROM document_retries r JOIN disclosures d ON d.id=r.disclosure_id "
                "WHERE d.source=? AND d.source_key=? AND d.record_hash=? AND r.record_hash=d.record_hash AND r.retry_policy_id=?",
                (item.source, item.identity, item.digest, policy),
            ).fetchone()
        return DocumentRetry(row[0], datetime.fromisoformat(row[1])) if row else None

    def document_ocr_timeouts(self, item: Disclosure, policy: str) -> int:
        with self._connect() as connection:
            row = connection.run(
                "SELECT r.ocr_timeout_attempts FROM document_retries r JOIN disclosures d ON d.id=r.disclosure_id "
                "WHERE d.source=? AND d.source_key=? AND d.record_hash=? AND r.record_hash=d.record_hash AND r.ocr_policy_id=?",
                (item.source, item.identity, item.digest, policy),
            ).fetchone()
        return int(row[0]) if row else 0

    def document_failure(
        self,
        item: Disclosure,
        policy: str,
        failure: str,
        attempted_at: datetime,
        retry_after: datetime,
        payload_hash: bytes | None = None,
        diagnostic: CompanyOcrDiagnostic | None = None,
        ocr_policy: str = "",
    ) -> None:
        encoded = json.dumps({
            "phase": diagnostic.phase, "page": diagnostic.page, "pages": diagnostic.pages,
            "characters": diagnostic.characters, "confidence": diagnostic.confidence,
            "segmentation_mode": diagnostic.segmentation_mode,
        }, separators=(",", ":"), allow_nan=False) if diagnostic is not None else None
        timeout = int(failure in {"disclosure_document_ocr_timeout", "disclosure_document_ocr_timeout_exhausted"})
        if self._raw_files is not None:
            self._raw_files.retain_failure(item, failure, policy, attempted_at.isoformat(), diagnostic)
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            connection.run(
                "INSERT INTO document_tasks(source,source_key,record_hash,stage) VALUES(?,?,?,'failed') "
                "ON CONFLICT(source,source_key,record_hash) DO UPDATE SET stage='failed'",
                (item.source, item.identity, item.digest),
            )
            connection.run(
                "INSERT INTO document_retries(disclosure_id,record_hash,retry_policy_id,failure,attempted_at,retry_at,attempts,payload_hash,ocr_diagnostic,ocr_policy_id,ocr_timeout_attempts) "
                "SELECT id,record_hash,?,?,?,?,1,?,?,?,? FROM disclosures WHERE source=? AND source_key=? AND record_hash=? "
                "ON CONFLICT(disclosure_id) DO UPDATE SET record_hash=excluded.record_hash,retry_policy_id=excluded.retry_policy_id,failure=excluded.failure,"
                "attempted_at=excluded.attempted_at,retry_at=excluded.retry_at,payload_hash=excluded.payload_hash,"
                "ocr_diagnostic=excluded.ocr_diagnostic,ocr_policy_id=excluded.ocr_policy_id,"
                "ocr_timeout_attempts=CASE WHEN document_retries.record_hash=excluded.record_hash "
                "AND document_retries.ocr_policy_id=excluded.ocr_policy_id THEN document_retries.ocr_timeout_attempts+excluded.ocr_timeout_attempts "
                "ELSE excluded.ocr_timeout_attempts END,"
                "attempts=CASE WHEN document_retries.record_hash=excluded.record_hash THEN document_retries.attempts+1 ELSE 1 END",
                (
                    policy,
                    failure,
                    attempted_at.isoformat(),
                    retry_after.isoformat(),
                    payload_hash,
                    encoded,
                    ocr_policy,
                    timeout,
                    item.source,
                    item.identity,
                    item.digest,
                ),
            )

    def component_due(self, component: str, scope: str, observed_at: datetime) -> bool:
        with self._connect() as connection:
            row = connection.run(
                "SELECT checked_at,failure FROM component_checks WHERE component=? AND scope=?", (component, scope)
            ).fetchone()
        ttl = timedelta(minutes=15) if component in {"financial", "news"} else timedelta(hours=4)
        return row is None or row[0] is None or bool(row[1]) or datetime.fromisoformat(row[0]) <= observed_at - ttl

    def component_since(self, component: str, scope: str, observed_at: datetime) -> date:
        if component == "financial":
            return observed_at.date() - timedelta(days=365)
        with self._connect() as connection:
            row = connection.run(
                "SELECT checked_at FROM component_checks WHERE component=? AND scope=?", (component, scope)
            ).fetchone()
        return max(observed_at.date() - timedelta(days=365), (
            datetime.fromisoformat(row[0]).date() - timedelta(days=7)
            if row and row[0]
            else observed_at.date() - timedelta(days=365)
        ))

    def annual_income_requests(
        self, codes: Sequence[str], observed_at: datetime
    ) -> tuple[CompanyAnnualIncomeRequest, ...]:
        with self._connect() as collected, SqliteSession(
            self.facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy,
        ) as formal:
            return PlanCompanyAnnualIncomeRequests(collected, formal, codes, observed_at)

    def commit_annual_income(
        self,
        request: CompanyAnnualIncomeRequest,
        incomes: tuple[CompanyAnnualIncome, ...],
        observed_at: datetime,
    ) -> int:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            changes = WriteCompanyAnnualIncome(connection, request, incomes, observed_at)
            if changes:
                MarkCompanyDirty(connection, (request.code,))
            return changes

    def structured_cursor(self, query: StructuredCompanyQuery) -> StructuredCompanyQuery:
        with self._connect() as connection:
            row = connection.run(
                "SELECT start_date,end_date,next_page,expected_rows,received_rows FROM structured_progress WHERE component=? AND scope='market'",
                (query.component,),
            ).fetchone()
        if row is not None and self._run_cutoff is not None and date.fromisoformat(row[0]) < self._run_cutoff.date() - timedelta(days=365):
            self._restart_structured(query.component, "market")
            return query
        return (
            replace(
                query,
                start=date.fromisoformat(row[0]),
                end=date.fromisoformat(row[1]),
                page=row[2],
                expected_rows=row[3],
                received_rows=row[4],
            )
            if row
            else query
        )

    def commit_structured(
        self, query: StructuredCompanyQuery, page: StructuredCompanyPage, observed_at: datetime
    ) -> int:
        start, end, received, scope = (
            query.start,
            query.end,
            query.received_rows,
            "market",
        )
        self._validate_structured_count(query, page)
        count = page.row_count
        members = tuple((item.code, item.report_date, None) for item in page.financials) + tuple(
            (item.code, item.day, item.ratio_pct) for item in page.ratios
        )
        keys = tuple((item[0], item[1]) for item in members)
        with self._connect() as connection:
            existing = set()
            if page.page > 1 and keys:
                # At most 500 supplier rows; read membership once instead of once per row.
                codes = tuple(sorted({code for code, _ in keys}))
                placeholders = ",".join("?" for _ in codes)
                existing = {
                    (row[0], date.fromisoformat(row[1]))
                    for row in connection.run(
                        f"SELECT code,as_of_date FROM structured_members WHERE component=? AND code IN ({placeholders})",
                        (page.component, *codes),
                    )
                }
            duplicate = len(keys) != len(set(keys)) or bool(existing.intersection(keys))
        if duplicate:
            self._restart_structured(page.component, scope)
            raise ValueError("structured_duplicate_page_rows")
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            if page.page == 1:
                connection.run("DELETE FROM structured_members WHERE component=?", (page.component,))
            connection.run_many(
                "INSERT INTO structured_members VALUES(?,?,?,?)",
                ((page.component, code, day.isoformat(), ratio) for code, day, ratio in members),
            )
            before = connection.total_changes
            financial_values = tuple(
                (
                    item.code,
                    item.report_date.isoformat(),
                    item.published_at.isoformat(),
                    item.basic_eps,
                    item.book_value_per_share,
                    item.revenue_growth_pct,
                    item.net_profit_growth_pct,
                    item.core_profit_growth_pct,
                    item.roe_pct,
                    item.parent_net_profit,
                    item.core_net_profit,
                )
                for item in page.financials
            )
            connection.run_many(
                "INSERT INTO financials VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(code,report_date) DO UPDATE SET "
                "published_at=excluded.published_at,eps=excluded.eps,bps=excluded.bps,"
                "revenue_growth_pct=excluded.revenue_growth_pct,profit_growth_pct=excluded.profit_growth_pct,"
                "core_profit_growth_pct=excluded.core_profit_growth_pct,roe_pct=excluded.roe_pct,"
                "parent_profit=excluded.parent_profit,core_profit=excluded.core_profit "
                "WHERE excluded.published_at>=published_at AND (published_at,eps,bps,revenue_growth_pct,profit_growth_pct,"
                "core_profit_growth_pct,roe_pct,parent_profit,core_profit) IS NOT "
                "(excluded.published_at,excluded.eps,excluded.bps,excluded.revenue_growth_pct,"
                "excluded.profit_growth_pct,excluded.core_profit_growth_pct,excluded.roe_pct,"
                "excluded.parent_profit,excluded.core_profit)",
                financial_values,
            )
            changes = connection.total_changes - before
            MarkCompanyDirty(connection, tuple(item.code for item in page.financials))
            if page.page == page.pages:
                connection.run("INSERT OR IGNORE INTO company_dirty_codes SELECT code FROM company_population")
                if page.component != "financial":
                    before = connection.total_changes
                    connection.run(
                        "DELETE FROM ratios WHERE component=? AND NOT EXISTS(SELECT 1 FROM structured_members m "
                        "WHERE m.component=ratios.component AND m.code=ratios.code AND m.as_of_date=ratios.as_of_date)",
                        (page.component,),
                    )
                    connection.run(
                        "INSERT INTO ratios SELECT component,code,as_of_date,ratio_pct FROM structured_members WHERE component=? "
                        "ON CONFLICT(component,code,as_of_date) DO UPDATE SET ratio_pct=excluded.ratio_pct WHERE ratio_pct<>excluded.ratio_pct",
                        (page.component,),
                    )
                    changes += connection.total_changes - before
                    connection.run(
                        "INSERT OR REPLACE INTO ratio_snapshots VALUES(?,?,?,?)",
                        (page.component, start.isoformat(), end.isoformat(), observed_at.isoformat()),
                    )
                connection.run("DELETE FROM structured_progress WHERE component=? AND scope=?", (page.component, scope))
                connection.run(
                    "INSERT OR REPLACE INTO component_checks VALUES(?,?,?,'')",
                    (
                        page.component,
                        scope,
                        min(observed_at, datetime.combine(end, observed_at.timetz())).isoformat()
                        if page.component == "financial"
                        else observed_at.isoformat(),
                    ),
                )
            else:
                connection.run(
                    "INSERT OR REPLACE INTO structured_progress VALUES(?,?,?,?,?,?,?)",
                    (
                        page.component,
                        scope,
                        start.isoformat(),
                        end.isoformat(),
                        page.page + 1,
                        page.total,
                        received + count,
                    ),
                )
        return changes

    def _validate_structured_count(self, query: StructuredCompanyQuery, page: StructuredCompanyPage) -> None:
        expected, received, scope = query.expected_rows, query.received_rows, "market"
        if query.component != page.component or query.page != page.page:
            raise ValueError("structured_query_mismatch")
        count = page.row_count
        if expected is not None and expected != page.total:
            self._restart_structured(page.component, scope)
            raise ValueError("structured_count_changed")
        if page.page == page.pages and received + count != page.total:
            self._restart_structured(page.component, scope)
            raise ValueError("structured_pages_incomplete")
        if page.page < page.pages and (not count or received + count >= page.total):
            self._restart_structured(page.component, scope)
            raise ValueError("structured_pages_inconsistent")

    def _restart_structured(self, component: str, scope: str) -> None:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            connection.run("DELETE FROM structured_progress WHERE component=? AND scope=?", (component, scope))
            connection.run("DELETE FROM structured_members WHERE component=?", (component,))

    def component_failure(self, component: str, scope: str, failure: str) -> None:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            connection.run(
                "INSERT INTO component_checks(component,scope,failure) VALUES(?,?,?) "
                "ON CONFLICT(component,scope) DO UPDATE SET failure=excluded.failure",
                (component, scope, failure),
            )
            if scope == "market":
                connection.run("INSERT OR IGNORE INTO company_dirty_codes SELECT code FROM company_population")
            else:
                MarkCompanyDirty(connection, (scope,))

    def commit_news(self, code: str, rows: Sequence[Disclosure], observed_at: datetime) -> int:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            changes = self._write_disclosures(connection, rows)
            connection.run(
                "INSERT OR REPLACE INTO component_checks VALUES('news',?,?,'')", (code, observed_at.isoformat())
            )
            MarkCompanyDirty(connection, (code,))
        return changes

    def prepare_publication(self, codes: Sequence[str], observed_at: datetime) -> None:
        self._run_cutoff = observed_at
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            connection.run("INSERT INTO company_sync_job VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET cutoff=excluded.cutoff",
                           (observed_at.isoformat(), observed_at.isoformat()))
            for offset in range(0, len(codes), 128):
                MarkCompanyDirty(connection, PendingCompanyContractLinkCodes(connection, codes[offset:offset + 128], self._facts.extraction_hash))
        if self._raw_files is not None:
            with self._connect() as connection:
                tasks = connection.run(
                    "SELECT d.*,MIN(c.code) AS code FROM document_tasks t JOIN disclosures d "
                    "ON d.source=t.source AND d.source_key=t.source_key AND d.record_hash=t.record_hash "
                    "JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE t.stage<>'cleaned' GROUP BY d.id"
                ).fetchall()
            for row in tasks:
                item = DecodeCompanyDisclosure(row)
                local = self._raw_files.local(item)
                with self._connect() as connection, SqliteTransaction(connection, immediate=True):
                    connection.run(
                        "UPDATE document_tasks SET stage=?,path=?,payload_hash=?,size=? WHERE source=? AND source_key=? AND record_hash=?",
                        ("downloaded" if local else "pending", str(local.path.relative_to(self.path.parent)) if local else None,
                         local.payload_hash if local else None, local.size if local else None, item.source, item.identity, item.digest),
                    )
        # Formal receipts are authoritative across a crash between the commits.
        with SqliteSession(self.facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as formal:
            with self._connect() as connection, SqliteTransaction(connection, immediate=True):
                RestoreCompanyFieldRepairs(connection, formal)
                for offset in range(0, len(codes), 128):
                    RegisterCompanyMaterialDependencies(
                        connection, formal, ReadCompanyFactRows(formal, codes[offset:offset + 128]), self._facts.extraction_hash,
                    )
                for row in formal.run("SELECT source,source_key,code,record_hash,extraction_hash,published_at,body_hash FROM company_input_receipt WHERE outcome='valid'"):
                    connection.run(
                        "INSERT OR REPLACE INTO document_publications SELECT id,?,?,?,? FROM disclosures "
                        "WHERE source=? AND source_key=? AND record_hash=? AND body_hash IS ? AND NOT EXISTS("
                        "SELECT 1 FROM company_field_repairs f WHERE f.disclosure_id=disclosures.id "
                        "AND f.code=? AND f.record_hash=disclosures.record_hash AND f.state='pending')",
                        (row[2], row[3], row[4], RequireCompanyTime(row[5]).isoformat(), row[0], row[1], row[3], row[6], row[2]),
                    )
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            PrepareCompanyPublication(connection, tuple(codes))
            connection.run(
                "UPDATE disclosures SET body_checked=1 WHERE EXISTS(SELECT 1 FROM document_publications p "
                "WHERE p.disclosure_id=disclosures.id AND p.record_hash=disclosures.record_hash AND p.extraction_hash=?) "
                "AND NOT EXISTS(SELECT 1 FROM disclosure_codes c JOIN company_population u ON u.code=c.code "
                "WHERE c.disclosure_id=disclosures.id AND NOT EXISTS(SELECT 1 FROM document_publications p "
                "WHERE p.disclosure_id=disclosures.id AND p.code=c.code AND p.record_hash=disclosures.record_hash AND p.extraction_hash=?))",
                (self._facts.extraction_hash, self._facts.extraction_hash),
            )
            connection.run(
                "UPDATE disclosures SET body_checked=0 WHERE body_checked=1 AND body_retained=0 AND EXISTS("
                "SELECT 1 FROM disclosure_codes c JOIN company_population u ON u.code=c.code "
                "WHERE c.disclosure_id=disclosures.id AND NOT EXISTS(SELECT 1 FROM document_publications p "
                "WHERE p.disclosure_id=disclosures.id AND p.code=c.code AND p.record_hash=disclosures.record_hash AND p.extraction_hash=?))",
                (self._facts.extraction_hash,),
            )
        # A new extraction identity requires fresh source verification, not old receipts.
        with SqliteSession(self.facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as formal:
            parent = ReadCompanyPublicationIdentity(formal)
            if parent is not None and parent.extraction_hash == self._facts.extraction_hash:
                with self._connect() as connection, SqliteTransaction(connection, immediate=True):
                    QueueCompanyFieldRepairs(connection, formal, codes, observed_at, self.path.parent)
                    QueueCompanyRepurchaseRechecks(
                        connection, formal, codes, observed_at, self._facts.extraction_hash,
                        self._event_policy, self._documents.read,
                    )
                    QueueCompanyReductionRechecks(
                        connection, formal, codes, observed_at, self._facts.extraction_hash,
                        self._event_policy, self._documents.read,
                    )
        if parent is not None and parent.extraction_hash != self._facts.extraction_hash:
            with self._connect() as connection, SqliteTransaction(connection, immediate=True):
                # Reuse retained text and immutable receipts; re-open only inputs without local text.
                connection.run("UPDATE disclosures SET body_checked=0 WHERE body_retained=0 AND kind='announcement'")
                MarkCompanyDirty(connection, tuple(codes))
        with self._connect() as connection:
            prepared = connection.run("SELECT 1 FROM disclosures WHERE body_retained=1 LIMIT 1").fetchone()
        if prepared is not None:
            # Drain a recovered batch before any new dispatch consumes temporary capacity.
            self.publish(codes, observed_at, changed_only=True)

    def published_count(self, codes: Sequence[str]) -> int:
        with SqliteSession(self.facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
            existing = {row[0] for row in connection.run("SELECT code FROM company_facts")}
        return len(existing.intersection(codes))

    def document_publication_urgent(self, item: Disclosure) -> bool:
        """A new input may satisfy an older queued field; publish before dispatching it."""
        with self._connect() as connection:
            for code in item.codes:
                if connection.run(
                    "SELECT 1 FROM company_field_repairs f JOIN disclosures d ON d.id=f.disclosure_id "
                    "AND d.record_hash=f.record_hash JOIN disclosure_codes c ON c.disclosure_id=d.id AND c.code=f.code "
                    "WHERE f.code=? AND f.state='pending' AND d.body_checked=0 "
                    "AND NOT(d.source=? AND d.source_key=? AND d.record_hash=?) LIMIT 1",
                    (code, item.source, item.identity, item.digest),
                ).fetchone() is not None:
                    return True
        return False

    def publish(self, codes: Sequence[str], observed_at: datetime, *, changed_only: bool = False) -> int:
        requested = tuple(sorted(set(codes)))
        if len(requested) > 12_000:
            raise ValueError("company_publication_population_exceeded")
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            connection.run_many("INSERT OR IGNORE INTO company_population VALUES(?)", ((code,) for code in requested))
        prepared: list[CompanyFacts] = []
        previous_facts: dict[str, CompanyFacts] = {}
        receipts: list[tuple[object, ...]] = []
        formal_receipts: list[CompanyInputReceipt] = []
        field_results: list[CompanyFieldConclusion] = []
        contract_refreshes: list[CompanyContractLinkRefresh] = []
        listing_dates = {item.code: item.listed_on for item in self._listings}
        with (
            SqliteSession(
                self.path,
                SqliteConnectionOptions(read_only=True),
                cancel_requested=self._cancel_requested, sqlite_policy=self._sqlite_policy,
            ) as source,
            ExitStack() as formal_scope,
        ):
            formal = formal_scope.enter_context(SqliteSession(
                self.facts_path,
                SqliteConnectionOptions(read_only=True),
                cancel_requested=self._cancel_requested, sqlite_policy=self._sqlite_policy,
            ))
            source_version = source.run("PRAGMA data_version").require_one()[0]
            with SqliteTransaction(source), SqliteTransaction(formal):
                parent = ReadCompanyPublicationIdentity(formal)
                rebuild = parent is not None and parent.extraction_hash != self._facts.extraction_hash
                existing = {row[0] for row in formal.run("SELECT code FROM company_facts")}
                if rebuild:
                    requested = tuple(sorted(set(requested) | existing))
                elif changed_only and parent is not None and parent.published_at.date() == observed_at.date():
                    dirty = {row[0] for row in source.run("SELECT code FROM company_dirty_codes")}
                    requested = tuple(code for code in requested if code in dirty or code not in existing)
                if not requested:
                    return 0
                sources: dict[str, str] = {}
                for row in formal.run("SELECT source,source_key FROM company_evidence_ref"):
                    BindCompanyEvidenceSource(sources, row[1], DecodeCompanyLabel(EVIDENCE_SOURCES, row[0]))
                shared = ReadCompanySnapshotShared(source, observed_at)
                for offset in range(0, len(requested), 128):
                    batch = requested[offset : offset + 128]
                    inputs = ReadCompanySnapshotRows(source, batch, observed_at, b"" if rebuild else self._facts.extraction_hash)
                    previous = {item.code: item for item in ReadCompanyFactRows(formal, batch)}
                    previous_facts.update(previous)
                    link_rows = ReadCompanyPublicationLinks(
                        source, batch, self._facts.extraction_hash, observed_at=observed_at,
                        event_policy=self._event_policy, read_document=self._documents.read,
                    ) if self._batch_publication_reads else ()
                    for code in batch:
                        self._check_cancelled()
                        rows = inputs[code].disclosures
                        for row in rows:
                            if row["kind"] == "announcement":
                                BindCompanyEvidenceSource(sources, row["source_key"], row["source"])
                        snapshot = BuildCompanySnapshot(
                            code,
                            observed_at,
                            inputs[code],
                            shared,
                            listed_on=listing_dates.get(code),
                            read_document=self._documents.read,
                        )
                        facts = CompileCompanyFacts(
                            snapshot, self._event_policy, previous=None if rebuild else previous.get(code)
                        )
                        link_input = (
                            next(item for item in link_rows if item.code == code)
                            if self._batch_publication_reads
                            else ReadCompanyPublicationLinks(
                                source, (code,), self._facts.extraction_hash, observed_at=observed_at,
                                event_policy=self._event_policy, read_document=self._documents.read,
                            )[0]
                        )
                        contract_refreshes.extend(link_input.refreshes)
                        review_keys = {(r.scope, r.reason_code, r.evidence_id) for r in facts.review_reasons}
                        recompiled = {
                            (r["source"], r["source_key"], r["record_hash"], r["body_hash"]) for r in rows
                            if r["kind"] == "announcement" and r["body_checked"] and r["body_retained"]
                            and not r["published_for_code"] and not r["superseded_for_code"] and CompanyDocumentNeedsReview(r["title"])
                        }
                        reviewed_inputs = {r.evidence_id for r in facts.review_reasons if r.scope == "positive"
                                           and r.reason_code in {"progress_unlinked", "resolution_unlinked", "cancellation_unlinked"}}
                        reviewed_inputs.difference_update(item[1] for item in recompiled)
                        reviewed_inputs.update(
                            r.binding.source_key for r in link_input.refreshes
                            if (r.binding.source, r.binding.source_key, r.binding.record_hash, r.binding.body_hash) not in recompiled
                        )
                        # Current compiled input owns its business conclusions;
                        # stale cache differences cannot add another blocker.
                        reviews = (*facts.review_reasons, *(r for r in link_input.reviews if r.evidence_id in reviewed_inputs
                                   and (r.scope, r.reason_code, r.evidence_id) not in review_keys))
                        candidates = LinkCompanyAnnouncementNumbers(source, code, link_input.candidates)
                        linked = ResolveCompanyLinks(candidates, facts.risk_events, facts.positive_events, reviews, self._event_policy)
                        facts = replace(facts, risk_events=tuple(event for event in linked.risks if event.expires_at is None or event.expires_at > observed_at),
                                        positive_events=ApplyCompanyAnnualIncome(code, linked.positives, inputs[code].annual_incomes, observed_at, financials=snapshot.financials), review_reasons=linked.reviews,
                                        announcement_state=CompanyDataState.COMPLETE if snapshot.announcement_complete and not any(r.scope == "risk" for r in linked.reviews) else CompanyDataState.PARTIAL)
                        prior = previous.get(code)
                        if prior is not None and replace(facts, published_at=prior.published_at) == prior:
                            facts = prior
                        prepared.append(facts)
                        fields = PrepareCompanyFieldResults(
                            source, formal, facts, snapshot.announcements, observed_at=observed_at,
                            extraction_hash=self._facts.extraction_hash, event_policy=self._event_policy, read_document=self._documents.read,
                        )
                        field_results.extend(fields.results)
                        formal_receipts.extend(fields.receipts)
                        receipts.extend(
                            (row["id"], code, row["record_hash"], self._facts.extraction_hash, observed_at.isoformat())
                            for row in rows
                            if row["kind"] == "announcement"
                            and row["body_checked"]
                            and row["body_retained"]
                            and not row["published_for_code"]
                        )
                        formal_receipts.extend(
                            CompanyInputReceipt(row["source"], row["source_key"], code, row["record_hash"],
                                                self._facts.extraction_hash, row["body_hash"], observed_at)
                            for row in rows if row["kind"] == "announcement" and row["body_checked"]
                            and row["body_retained"] and not row["published_for_code"]
                        )
                        formal_receipts.extend(
                            CompanyInputReceipt(
                                "eastmoney_annual_income",
                                income.source_key,
                                code,
                                income.record_hash,
                                self._facts.extraction_hash,
                                None,
                                observed_at,
                            )
                            for income in inputs[code].annual_incomes
                        )
                        formal_receipts.extend(
                            CompanyInputReceipt(row["source"], row["source_key"], code, row["record_hash"], self._facts.extraction_hash,
                                                row["payload_hash"], observed_at, row["document_failure"], row["retry_policy_id"])
                            for row in rows if row["document_failure"] and not IsTransientCompanyDocumentFailure(row["document_failure"])
                        )
                        if self._stock_progress is not None:
                            self._stock_progress(
                                CompanyStockProgress(
                                    code,
                                    processed=len(prepared),
                                    total=len(requested),
                                    announcements=len(rows),
                                    analyzed=sum(bool(row["body_checked"]) for row in rows),
                                    pending=facts.pending_document_count,
                                    risks=len(facts.risk_events),
                                    positives=len(facts.positive_events),
                                    coverage_complete=all(
                                        state is CompanyDataState.COMPLETE
                                        for state in (
                                            facts.financial_state,
                                            facts.pledge_state,
                                            facts.unlock_state,
                                            facts.announcement_state,
                                        )
                                    ),
                                    reused=facts is prior,
                                    review_count=len(facts.review_reasons),
                                )
                            )
            # A retained read handle prevents the writer's last close from
            # checkpointing WAL before its file fingerprint is published.
            formal_scope.close()
            self._check_cancelled()
            # Pin the sole collection writer across verification, formal commit and receipt creation.
            with self._connect() as connection, SqliteTransaction(connection, immediate=True):
                if source.run("PRAGMA data_version").require_one()[0] != source_version:
                    raise RuntimeError("company_publication_source_changed")
                RegisterCompanyFieldAttempts(connection, prepared, formal_receipts, self._facts.extraction_hash)
                self._facts.publish(
                    tuple(item for item in prepared if item is not previous_facts.get(item.code)),
                    sources,
                    parent,
                    observed_at,
                    receipts=(*formal_receipts, *CompanyFieldRepairReceipts(connection, requested, observed_at)),
                    field_results=field_results,
                )
                connection.run_many("INSERT OR REPLACE INTO document_publications VALUES(?,?,?,?,?)", receipts)
                ApplyCompanyContractLinkRefreshes(connection, contract_refreshes)
                with SqliteSession(self.facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as formal:
                    repaired_codes = CompleteCompanyFieldRepairs(connection, formal, prepared, self._facts.extraction_hash)
                    RegisterCompanyMaterialDependencies(connection, formal, prepared, self._facts.extraction_hash)
                connection.run_many("DELETE FROM company_dirty_codes WHERE code=?", ((code,) for code in requested))
                superseded_codes = tuple(sorted(set(repaired_codes) | set(
                    PublishCompanySupersessions(connection, prepared, self._facts.extraction_hash))))
                MarkCompanyDirty(connection, superseded_codes)
                ReleasePublishedCompanyMaterial(connection, self._facts.extraction_hash)
        self._release_files()
        if superseded_codes:
            self.publish(superseded_codes, observed_at, changed_only=True)
        if not changed_only:
            self.finish(observed_at)
        return len(requested)

    def _release_files(self) -> None:
        with (
            self._connect() as connection,
            SqliteTransaction(connection, immediate=True),
            SqliteSession(self.facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as formal,
        ):
            ReleaseCompanyMaterialFiles(
                connection, formal, self._documents, self._raw_files, self._facts.extraction_hash, self._run_cutoff,
            )

    def finish(self, observed_at: datetime) -> None:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            cutoff = (observed_at.date() - timedelta(days=365)).isoformat()
            ReleaseCompletedCompanyQueryRows(connection)
            connection.run("DELETE FROM partitions WHERE end_date<? AND is_complete=1", (cutoff,))
            connection.run("DELETE FROM disclosure_query_contracts WHERE end_date<? AND NOT EXISTS(SELECT 1 FROM partitions p WHERE p.market=disclosure_query_contracts.market AND p.start_date=disclosure_query_contracts.start_date AND p.end_date=disclosure_query_contracts.end_date AND p.is_complete=0)", (cutoff,))
            connection.run("DELETE FROM partition_rows WHERE end_date<? AND NOT EXISTS(SELECT 1 FROM partitions p WHERE p.market=partition_rows.market AND p.start_date=partition_rows.start_date AND p.end_date=partition_rows.end_date AND p.is_complete=0)", (cutoff,))
            connection.run(
                "DELETE FROM financials WHERE report_date<?", ((observed_at.date() - timedelta(days=550)).isoformat(),)
            )
            ReleasePublishedCompanyMaterial(connection, self._facts.extraction_hash)
        self._release_files()
        with self._connect() as connection:
            connection.run("PRAGMA incremental_vacuum(256)")


def _key(partition: DisclosurePartition) -> tuple[str, str, str]:
    return partition.market, partition.start.isoformat(), partition.end.isoformat()


__all__ = ["SQLiteCompanyEvidenceIndex"]
