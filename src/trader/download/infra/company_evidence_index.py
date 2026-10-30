"""Single-writer incremental evidence index and transactional published read model."""

from __future__ import annotations

import sqlite3
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

from trader.download.domain.company_events import CompanyDataState, CompanyEventPolicy
from trader.download.domain.company_evidence import (
    CompanyDocumentExtraction,
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
from trader.download.infra.company_evidence_sql import (
    DISCLOSURE_QUERY_SCHEMA,
    DOCUMENT_EXTRACTION_SCHEMA,
    DOCUMENT_RETRY_SCHEMA,
    SCHEMA,
)
from trader.download.infra.company_fact_inputs import (
    BuildCompanySnapshot,
    DecodeCompanyDisclosure,
    ReadCompanySnapshotRows,
    ReadCompanySnapshotShared,
)
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity, SQLiteCompanyFactsIndex
from trader.download.infra.company_facts_protocol import EVIDENCE_SOURCES, DecodeCompanyLabel
from trader.download.infra.company_pipeline_queries import (
    CheckDisclosureQueryContract,
    EnsureCompanyPipelineQueries,
    IndexCompanyDocument,
    MarkCompanyDirty,
    MarkCompanyMarketDirty,
    PrepareCompanyPublication,
    ReleaseCompletedCompanyQueryRows,
    ReleaseUnusedCompanyDisclosures,
    SelectCompanyDocuments,
)
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)

_DEFAULT_EVENT_POLICY = CompanyEventPolicy()


class SQLiteCompanyEvidenceIndex:
    """Own writes to the fresh database; all connections have bounded lifetimes."""

    def __init__(
        self,
        path: Path,
        *,
        facts_path: Path,
        documents: CompanyDocumentFiles,
        cancel_requested: Callable[[], bool] | None = None,
        event_policy: CompanyEventPolicy = _DEFAULT_EVENT_POLICY,
        stock_progress: Callable[[CompanyStockProgress], None] | None = None,
        listings: tuple[CompanyListing, ...] = (),
    ) -> None:
        self.path = path
        if path.resolve() == facts_path.resolve():
            raise ValueError("company_paths_must_be_separate")
        self.facts_path = facts_path
        self._documents = documents
        self._facts = SQLiteCompanyFactsIndex(facts_path, event_policy=event_policy, cancel_requested=cancel_requested)
        self._cancel_requested = cancel_requested
        self._event_policy = event_policy
        self._stock_progress = stock_progress
        if len({item.code for item in listings}) != len(listings):
            raise ValueError("company_listing_identity_invalid")
        self._listings = listings

    def _check_cancelled(self) -> None:
        if self._cancel_requested is not None and self._cancel_requested():
            raise InterruptedError("company_sync_cancelled")

    @contextmanager
    def _connect(self, *, accepted_document: bool = False) -> Iterator[SqliteConnection]:
        if not accepted_document:
            self._check_cancelled()
        try:
            with SqliteSession(
                self.path,
                SqliteConnectionOptions(timeout_seconds=8, foreign_keys=True),
                cancel_requested=None if accepted_document else self._cancel_requested,
            ) as connection:
                yield connection
        except sqlite3.Error as exc:
            if str(exc) == "interrupted" and self._cancel_requested is not None and self._cancel_requested():
                raise InterruptedError("company_sync_cancelled") from exc
            raise RuntimeError("company_evidence_persistence_unavailable") from exc

    def initialize(self) -> None:
        self._check_cancelled()
        self._facts.initialize()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.path.exists():
                with SqliteSession(
                    self.path, SqliteConnectionOptions(read_only=True), cancel_requested=self._cancel_requested
                ) as connection:
                    if connection.run("SELECT 1 FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT 1").fetchone():
                        self._validate_contract(connection)
            with SqliteSession(
                self.path,
                SqliteConnectionOptions(journal_mode="WAL", timeout_seconds=8, foreign_keys=True),
                cancel_requested=self._cancel_requested,
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
                    retry_columns = {
                        str(row[1]) for row in connection.run("PRAGMA table_info(document_retries)").fetchall()
                    }
                    if "payload_hash" not in retry_columns:
                        connection.run(
                            "ALTER TABLE document_retries ADD COLUMN payload_hash BLOB "
                            "CHECK(payload_hash IS NULL OR length(payload_hash)=32)"
                        )
                    for column, declaration in (
                        ("ocr_diagnostic", "TEXT"),
                        ("ocr_policy_id", "TEXT NOT NULL DEFAULT ''"),
                        ("ocr_timeout_attempts", "INTEGER NOT NULL DEFAULT 0"),
                    ):
                        if column not in retry_columns:
                            connection.run(f"ALTER TABLE document_retries ADD COLUMN {column} {declaration}")
                    connection.run(DOCUMENT_EXTRACTION_SCHEMA)
                    for statement in DISCLOSURE_QUERY_SCHEMA:
                        connection.run(statement)
                    ActivateDisclosureQueryPolicy(connection)
                    EnsureCompanyPipelineQueries(connection)
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
        if connection.run("PRAGMA user_version").require_one()[0] != SCHEMA.version:
            raise ValueError("company_evidence_database_contract_mismatch")
        contract = connection.run("SELECT schema_id FROM contract").fetchall()
        if [row[0] for row in contract] != ["company_collection"]:
            raise ValueError("company_evidence_database_contract_mismatch")

    def _repair_pending_files(self) -> None:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            for row in connection.run("SELECT id,body_hash FROM disclosures WHERE body_retained=1").fetchall():
                try:
                    self._documents.read(row[1])
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
        self._release_files()

    def partitions(self, markets: Sequence[Market], observed_at: datetime) -> tuple[DisclosurePartition, ...]:
        today = observed_at.date()
        cutoff = today - timedelta(days=365)
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            pending_before: set[tuple[str, str, str]] = set()
            for market in markets:
                before_planning = connection.total_changes
                pending_before.update(
                    (row["market"], row["start_date"], row["end_date"])
                    for row in connection.run(
                        "SELECT market,start_date,end_date FROM partitions WHERE market=? AND is_complete=0 AND end_date>=?",
                        (market, cutoff.isoformat()),
                    ).fetchall()
                )
                latest = connection.run(
                    "SELECT MAX(end_date) FROM partitions WHERE market=? AND is_complete=1", (market,)
                ).require_one()[0]
                baseline_start = max(cutoff, date.fromisoformat(latest) + timedelta(days=1)) if latest else cutoff
                for start, end in DisclosureMonths(baseline_start, today - timedelta(days=7)):
                    self._add_partition(connection, market, start, end)
                # Check each recent day separately: fixed pages survive interruption and don't shift at midnight.
                for offset in range(7):
                    day = today - timedelta(days=offset)
                    # Completed wider slices also own their dates; do not create overlapping owners.
                    self._add_partition(connection, market, day, day)
                # One old month per day, excluding the recent overlap. Bound supplier correction audits.
                months = DisclosureMonths(cutoff, today - timedelta(days=7))
                if months and not pending_before:
                    start, end = months[today.toordinal() % len(months)]
                    self._add_partition(connection, market, start, end)
                    # Reopened audits must fetch fresh children rather than reconcile yesterday's set.
                    connection.run(
                        "DELETE FROM disclosure_queries WHERE market=? AND EXISTS(SELECT 1 FROM partitions p "
                        "WHERE p.market=disclosure_queries.market AND p.start_date=disclosure_queries.start_date "
                        "AND p.end_date=disclosure_queries.end_date AND p.is_complete=1 AND ((p.end_date>=? AND p.checked_at<?) "
                        "OR (p.start_date>=? AND p.end_date<=? AND p.checked_at<?)))",
                        (
                            market,
                            (today - timedelta(days=6)).isoformat(),
                            today.isoformat(),
                            start.isoformat(),
                            end.isoformat(),
                            today.isoformat(),
                        ),
                    )
                    connection.run(
                        "UPDATE partitions SET next_page=1,expected_rows=NULL,received_rows=0,is_complete=0 "
                        "WHERE market=? AND is_complete=1 AND ((end_date>=? AND checked_at<?) "
                        "OR (start_date>=? AND end_date<=? AND checked_at<?))",
                        (
                            market,
                            (today - timedelta(days=6)).isoformat(),
                            today.isoformat(),
                            start.isoformat(),
                            end.isoformat(),
                            today.isoformat(),
                        ),
                    )
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

    def disclosure_queries(self, partition: DisclosurePartition) -> tuple[DisclosurePartition, ...] | None:
        with self._connect() as connection:
            return ReadDisclosureQueries(connection, replace(partition, query=None))

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

    def partition_retry_after(self, partition: DisclosurePartition) -> datetime | None:
        with self._connect() as connection:
            row = connection.run(
                "SELECT retry_at FROM partition_retries WHERE market=? AND start_date=? AND end_date=? AND scope=?",
                DisclosureQueryKey(partition),
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

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
        affected = tuple(
            item
            for item in rows
            if connection.run(
                "SELECT 1 FROM disclosures WHERE source=? AND source_key=? AND record_hash=?",
                (item.source, item.identity, item.digest),
            ).fetchone()
            is None
        )
        for item in affected:
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
    ) -> tuple[CompanyDocumentTask, ...]:
        if limit is not None and not 1 <= limit <= 200:
            raise ValueError("company_document_batch_invalid")
        with self._connect() as connection:
            SelectCompanyDocuments(connection, codes, observed_at, policies, excluded, repair)
            rows = connection.run(
                "SELECT d.source,d.source_key,d.kind,d.title,d.published_at,d.url,d.record_hash,c.code,p.route FROM disclosures d "
                "JOIN disclosure_codes c ON c.disclosure_id=d.id JOIN temp.pending_documents p ON p.id=d.id WHERE d.id IN ("
                "SELECT selected.id FROM temp.pending_documents selected JOIN disclosures s ON s.id=selected.id "
                "WHERE (?=0 OR selected.due=1) ORDER BY s.published_at DESC,s.id LIMIT ?) "
                "ORDER BY d.published_at DESC,d.id,c.code",
                (limit is not None or bool(excluded) or repair is not None, -1 if limit is None else limit),
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
            SelectCompanyDocuments(connection, codes, observed_at, policies, excluded, repair)
            row = connection.run(
                "SELECT COUNT(*),COALESCE(SUM(due),0),COALESCE(SUM(cooling),0),COALESCE(SUM(ocr),0),"
                "COALESCE(SUM(unextractable),0),MIN(CASE WHEN cooling=1 THEN retry_at END) FROM temp.pending_documents"
            ).require_one()
        return CompanyDocumentWorkload(
            row[0], row[1], row[2], row[3], row[4], datetime.fromisoformat(row[5]) if row[5] else None
        )

    def commit_document(self, item: Disclosure, extraction: CompanyDocumentExtraction, extracted_at: datetime) -> bool:
        # A result already accepted by the main thread is drained on cancellation.
        with self._connect(accepted_document=True) as connection, SqliteTransaction(connection, immediate=True):
            digest = self._documents.write(extraction.text)
            changed = connection.run(
                "UPDATE disclosures SET body_hash=?,body_checked=1,body_retained=1 WHERE source=? AND source_key=? AND record_hash=? AND body_checked=0",
                (digest, item.source, item.identity, item.digest),
            ).rowcount
            if not changed:
                return False
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
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
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
        with self._connect() as connection:
            row = connection.run(
                "SELECT checked_at FROM component_checks WHERE component=? AND scope=?", (component, scope)
            ).fetchone()
        return (
            datetime.fromisoformat(row[0]).date() - timedelta(days=7)
            if row and row[0]
            else observed_at.date() - timedelta(days=550)
        )

    def structured_cursor(self, query: StructuredCompanyQuery) -> StructuredCompanyQuery:
        with self._connect() as connection:
            row = connection.run(
                "SELECT start_date,end_date,next_page,expected_rows,received_rows FROM structured_progress WHERE component=? AND scope='market'",
                (query.component,),
            ).fetchone()
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
                "WHERE (published_at,eps,bps,revenue_growth_pct,profit_growth_pct,"
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
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            PrepareCompanyPublication(connection, tuple(codes))
            connection.run(
                "UPDATE disclosures SET body_checked=0 WHERE body_checked=1 AND body_retained=0 AND EXISTS("
                "SELECT 1 FROM disclosure_codes c JOIN company_population u ON u.code=c.code "
                "WHERE c.disclosure_id=disclosures.id AND NOT EXISTS(SELECT 1 FROM document_publications p "
                "WHERE p.disclosure_id=disclosures.id AND p.code=c.code AND p.record_hash=disclosures.record_hash AND p.extraction_hash=?))",
                (self._facts.extraction_hash,),
            )
        # A new extraction identity requires fresh source verification, not old receipts.
        with SqliteSession(self.facts_path, SqliteConnectionOptions(read_only=True)) as formal:
            parent = ReadCompanyPublicationIdentity(formal)
        if parent is not None and parent.extraction_hash != self._facts.extraction_hash:
            with self._connect() as connection, SqliteTransaction(connection, immediate=True):
                connection.run("DELETE FROM document_publications")
                connection.run("UPDATE disclosures SET body_checked=0,body_retained=0,body_hash=NULL")
                connection.run("DELETE FROM partitions")
                connection.run("DELETE FROM coverage")
                connection.run("DELETE FROM partition_rows")
                MarkCompanyDirty(connection, tuple(codes))
        with self._connect() as connection:
            prepared = connection.run("SELECT 1 FROM disclosures WHERE body_retained=1 LIMIT 1").fetchone()
        if prepared is not None:
            # Drain a recovered batch before any new dispatch consumes temporary capacity.
            self.publish(codes, observed_at, changed_only=True)

    def published_count(self, codes: Sequence[str]) -> int:
        with SqliteSession(self.facts_path, SqliteConnectionOptions(read_only=True)) as connection:
            existing = {row[0] for row in connection.run("SELECT code FROM company_facts")}
        return len(existing.intersection(codes))

    def publish(self, codes: Sequence[str], observed_at: datetime, *, changed_only: bool = False) -> int:
        requested = tuple(sorted(set(codes)))
        if len(requested) > 12_000:
            raise ValueError("company_publication_population_exceeded")
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            connection.run_many("INSERT OR IGNORE INTO company_population VALUES(?)", ((code,) for code in requested))
        prepared: list[CompanyFacts] = []
        previous_facts: dict[str, CompanyFacts] = {}
        receipts: list[tuple[object, ...]] = []
        listing_dates = {item.code: item.listed_on for item in self._listings}
        with (
            SqliteSession(
                self.path,
                SqliteConnectionOptions(read_only=True, timeout_seconds=8),
                cancel_requested=self._cancel_requested,
            ) as source,
            SqliteSession(
                self.facts_path,
                SqliteConnectionOptions(read_only=True, timeout_seconds=8),
                cancel_requested=self._cancel_requested,
            ) as formal,
        ):
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
                    self._bind_evidence(sources, row[1], DecodeCompanyLabel(EVIDENCE_SOURCES, row[0]))
                shared = ReadCompanySnapshotShared(source, observed_at)
                for offset in range(0, len(requested), 128):
                    batch = requested[offset : offset + 128]
                    inputs = ReadCompanySnapshotRows(source, batch, observed_at, self._facts.extraction_hash)
                    previous = {item.code: item for item in ReadCompanyFactRows(formal, batch)}
                    previous_facts.update(previous)
                    for code in batch:
                        self._check_cancelled()
                        rows = inputs[code].disclosures
                        for row in rows:
                            if row["kind"] == "announcement":
                                self._bind_evidence(sources, row["source_key"], row["source"])
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
                        prior = previous.get(code)
                        if prior is not None and replace(facts, published_at=prior.published_at) == prior:
                            facts = prior
                        prepared.append(facts)
                        receipts.extend(
                            (row["id"], code, row["record_hash"], self._facts.extraction_hash, observed_at.isoformat())
                            for row in rows
                            if row["kind"] == "announcement"
                            and row["body_checked"]
                            and row["body_retained"]
                            and not row["published_for_code"]
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
            self._check_cancelled()
            # Pin the sole collection writer across verification, formal commit and receipt creation.
            with self._connect() as connection, SqliteTransaction(connection, immediate=True):
                if source.run("PRAGMA data_version").require_one()[0] != source_version:
                    raise RuntimeError("company_publication_source_changed")
                self._facts.publish(
                    tuple(item for item in prepared if item is not previous_facts.get(item.code)),
                    sources,
                    parent,
                    observed_at,
                )
                connection.run_many("INSERT OR REPLACE INTO document_publications VALUES(?,?,?,?,?)", receipts)
                connection.run_many("DELETE FROM company_dirty_codes WHERE code=?", ((code,) for code in requested))
                self._revisit_unlinked(connection, prepared, previous_facts)
                self._release_published_documents(connection)
        self._release_files()
        if not changed_only:
            self.finish(observed_at)
        return len(requested)

    @staticmethod
    def _bind_evidence(sources: dict[str, str], key: str, source: str) -> None:
        previous = sources.get(key)
        if previous is not None and previous != source:
            raise ValueError("company_evidence_source_ambiguous")
        sources[key] = source

    @staticmethod
    def _revisit_unlinked(
        connection: SqliteConnection, facts: Sequence[CompanyFacts], previous: dict[str, CompanyFacts]
    ) -> None:
        for item in facts:
            old = previous.get(item.code)
            old_events = (
                ({event.event_id for event in old.risk_events} | {event.event_id for event in old.positive_events})
                if old
                else set()
            )
            new_events = {event.event_id for event in item.risk_events} | {
                event.event_id for event in item.positive_events
            }
            if not new_events.difference(old_events):
                continue
            for review in item.review_reasons:
                if (
                    review.reason_code not in {"resolution_unlinked", "progress_unlinked", "cancellation_unlinked"}
                    or review.evidence_id is None
                ):
                    continue
                for row in connection.run(
                    "SELECT d.id FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                    "WHERE c.code=? AND d.source_key=?",
                    (item.code, review.evidence_id),
                ).fetchall():
                    connection.run(
                        "DELETE FROM document_publications WHERE disclosure_id=? AND code=?", (row[0], item.code)
                    )
                    connection.run("UPDATE disclosures SET body_checked=body_retained WHERE id=?", (row[0],))
                    MarkCompanyDirty(connection, (item.code,))

    def _release_published_documents(self, connection: SqliteConnection) -> None:
        connection.run(
            "UPDATE disclosures SET body_retained=0 WHERE body_retained=1 AND NOT EXISTS("
            "SELECT 1 FROM disclosure_codes c JOIN company_population u ON u.code=c.code "
            "WHERE c.disclosure_id=disclosures.id AND NOT EXISTS("
            "SELECT 1 FROM document_publications p WHERE p.disclosure_id=disclosures.id "
            "AND p.code=c.code AND p.record_hash=disclosures.record_hash AND p.extraction_hash=?))",
            (self._facts.extraction_hash,),
        )
        # Commit the receipt before physical deletion; recovery can repeat this idempotently.

    def _release_files(self) -> None:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            retained = frozenset(
                row[0]
                for row in connection.run(
                    "SELECT DISTINCT body_hash FROM disclosures WHERE body_retained=1 AND body_hash IS NOT NULL"
                )
            )
            self._documents.release(retained)
            with SqliteSession(self.facts_path, SqliteConnectionOptions(read_only=True)) as formal:
                referenced = tuple(
                    (DecodeCompanyLabel(EVIDENCE_SOURCES, row[0]), row[1])
                    for row in formal.run("SELECT source,source_key FROM company_evidence_ref")
                )
            ReleaseUnusedCompanyDisclosures(connection, referenced)

    def finish(self, observed_at: datetime) -> None:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            cutoff = (observed_at.date() - timedelta(days=365)).isoformat()
            ReleaseCompletedCompanyQueryRows(connection)
            connection.run("DELETE FROM partitions WHERE end_date<?", (cutoff,))
            connection.run("DELETE FROM disclosure_query_contracts WHERE end_date<?", (cutoff,))
            connection.run("DELETE FROM partition_rows WHERE end_date<?", (cutoff,))
            connection.run(
                "DELETE FROM financials WHERE report_date<?", ((observed_at.date() - timedelta(days=550)).isoformat(),)
            )
            self._release_published_documents(connection)
        self._release_files()
        with self._connect() as connection:
            connection.run("PRAGMA incremental_vacuum(256)")


def _key(partition: DisclosurePartition) -> tuple[str, str, str]:
    return partition.market, partition.start.isoformat(), partition.end.isoformat()


__all__ = ["SQLiteCompanyEvidenceIndex"]
