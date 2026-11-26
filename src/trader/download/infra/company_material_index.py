"""Explicit bounded directory and document writes; no company job or formal publication."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path

from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.download.domain.company_announcement_numbers import ExtractCompanyAnnouncementNumbers
from trader.download.domain.company_event_extraction import ExtractCompanyLinkCandidate
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_evidence import (
    CompanyDocument, CompanyDocumentExtraction, CompanyInputValidationFailure, CompanyOcrDiagnostic,
    Disclosure, DisclosureCoverageMismatch, DisclosurePage, DisclosurePartition, DisclosurePartitionRetry,
    DisclosureQuery, DisclosureSecurity, DocumentRetry, EvidenceMarket, Market,
)
from trader.download.domain.download_failure import DownloadFailureConfiguration, DownloadFailureRecord
from trader.download.infra.company_announcement_links import WriteCompanyAnnouncementNumbers
from trader.download.infra.company_contract_link_rules import WriteCompanyContractLinkRules
from trader.download.infra.company_disclosure_queries import (
    ActivateDisclosureQueryPolicy, CommitDisclosureQueryPage, DeferDisclosurePartition, DisclosureQueryKey,
    ReadDisclosureQueries, ReconcileDisclosureQueries, RestartDisclosureQuery, SeedDisclosureQueries,
    ValidateDisclosureQueryPage,
)
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_document_recovery import RepairCompanyDocumentFiles
from trader.download.infra.company_material_cleanup import ReleaseCompanyMaterialFiles
from trader.download.infra.company_source_schema import COMPANY_SOURCE_SCHEMA
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_field_recovery import RecoverRequiredCompanyText
from trader.download.infra.company_link_codec import EncodeCompanyLink
from trader.download.infra.company_pipeline_queries import (
    CheckDisclosureQueryContract, IndexCompanyDocument, MarkCompanyDirty, MarkCompanyMarketDirty, PrepareCompanyPipelineQueries,
    ReplaceCompanyMaterialPopulation,
)
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.infra.download_failure_diagnostics import WriteDownloadFailure
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import EnsureSqliteSchema, SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy


class SQLiteCompanyMaterialIndex:
    def __init__(
        self,
        path: Path,
        *,
        facts: SQLiteCompanyFactsIndex,
        documents: CompanyDocumentFiles,
        event_policy: CompanyEventPolicy,
        sqlite_policy: SqliteIoPolicy,
        raw_files: CompanyRawFiles | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        io_budget: CompanySyncIoBudget | None = None,
    ) -> None:
        if path.resolve() == facts.path.resolve() or path.exists() and facts.path.exists() and path.samefile(facts.path):
            raise ValueError("company_material_paths_must_differ")
        self.path, self._facts, self.facts_path = path, facts, facts.path
        self._documents, self._raw_files = documents, raw_files
        self.raw_root = documents.root.parent
        self._event_policy, self._sqlite_policy, self._cancel_requested = event_policy, sqlite_policy, cancel_requested
        self._io_budget = io_budget

    def pending_registered_documents(self, codes: tuple[str, ...], cutoff: datetime) -> int:
        """Count the frozen population's registered single-issuer notices awaiting analysis."""
        window = ((cutoff.date() - timedelta(days=365)).isoformat(), cutoff.isoformat())
        total = 0
        with self._session(self.path, SqliteConnectionOptions(read_only=True),
            sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            for offset in range(0, len(codes), 512):
                batch = codes[offset : offset + 512]
                slots = ",".join("?" for _ in batch)
                row = connection.run(f"""
                    SELECT COUNT(*) FROM company_document_requirements required
                    JOIN disclosures d ON d.id=required.disclosure_id
                    JOIN disclosure_codes c ON c.disclosure_id=d.id
                    WHERE d.source='cninfo' AND d.kind='announcement' AND d.body_checked=0
                      AND d.published_at BETWEEN ? AND ? AND c.code IN ({slots})
                      AND (SELECT COUNT(*) FROM disclosure_codes AS members WHERE members.disclosure_id=d.id)=1
                    """, (*window, *batch)).require_one()
                total += int(row[0])
        return total

    @contextmanager
    def _session(self, path: Path, options: SqliteConnectionOptions | None = None, *,
        sqlite_policy: SqliteIoPolicy, cancel_requested: Callable[[], bool] | None = None) -> Iterator[SqliteConnection]:
        actual_options = SqliteConnectionOptions() if options is None else options
        if self._io_budget is not None:
            with self._io_budget.session(path, actual_options, sqlite_policy=sqlite_policy, cancel_requested=cancel_requested) as connection:
                yield connection
        else:
            with SqliteSession(path, actual_options, sqlite_policy=sqlite_policy, cancel_requested=cancel_requested) as connection:
                yield connection

    def initialize(self) -> None:
        self._check_cancelled()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            EnsureSqliteSchema(connection, COMPANY_SOURCE_SCHEMA, allow_create=True, exact=True)
            if connection.run("PRAGMA journal_mode=WAL").require_one()[0] != "wal":
                raise RuntimeError("company_material_journal_unavailable")
            with SqliteTransaction(connection, immediate=True):
                ActivateDisclosureQueryPolicy(connection)
                PrepareCompanyPipelineQueries(connection)

    def validate_material(self) -> None:
        """Explicitly validate retained originals, preserving unavailable original dependencies."""
        RepairCompanyDocumentFiles(self._connect, self._documents, self._check_cancelled)

    def validate_existing(self) -> None:
        if self.path.exists():
            with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
                EnsureSqliteSchema(connection, COMPANY_SOURCE_SCHEMA, exact=True)

    def clean_material(self, cutoff: datetime) -> None:
        """Run physical maintenance independently of any formal publication."""
        self._check_cancelled()
        if cutoff.utcoffset() is None:
            raise ValueError("company_material_time_invalid")
        ReleaseCompanyMaterialFiles(self._connect, self._facts, self._documents, self._raw_files,
                                    cutoff, sqlite_policy=self._sqlite_policy)

    def register_disclosures(self, rows: Sequence[Disclosure]) -> int:
        if not 1 <= len(rows) <= 500:
            raise ValueError("company_material_batch_invalid")
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            return self._write_disclosures(connection, rows)

    def set_population(self, codes: tuple[str, ...]) -> None:
        if codes != tuple(sorted(set(codes))) or any(len(code) != 6 or not code.isascii() or not code.isdigit() for code in codes):
            raise ValueError("company_material_population_invalid")
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            ReplaceCompanyMaterialPopulation(connection, codes)

    def register_partition(self, partition: DisclosurePartition, observed_at: datetime) -> None:
        if observed_at.utcoffset() is None or partition.start < observed_at.date() - timedelta(days=365) or partition.end > observed_at.date():
            raise ValueError("company_material_partition_window_invalid")
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            if partition.start == observed_at.date() - timedelta(days=365) and partition.end == observed_at.date() - timedelta(days=1):
                edges = _unstarted_edges(connection, partition.market, partition.start)
                if edges is None:
                    raise ValueError("company_material_window_requires_prior_cursor")
                for key in edges:
                    connection.run("DELETE FROM disclosure_query_contracts WHERE market=? AND start_date=? AND end_date=?", key)
                    connection.run("DELETE FROM partitions WHERE market=? AND start_date=? AND end_date=?", key)
                _reopen_intraday_coverage(connection, partition.market, partition.start, partition.end)
            self._add_partition(connection, partition.market, partition.start, partition.end)

    def recheck_prior_intraday(self, markets: tuple[Market, ...], day: date) -> None:
        """Withdraw provisional prior-day coverage before the new scope checks fields."""
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            for market in markets:
                _reopen_intraday_coverage(connection, market, day - timedelta(days=365), day - timedelta(days=1))

    def can_retain_backfill(self, markets: tuple[Market, ...], previous_day: date, next_day: date) -> bool:
        with self._session(self.path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection, SqliteTransaction(connection):
            for market in markets:
                key = (market, previous_day.isoformat(), previous_day.isoformat())
                if connection.run("""SELECT 1 FROM partitions p JOIN coverage c USING(market,start_date,end_date)
                    WHERE market=? AND start_date=? AND end_date=? AND p.is_complete=1""", key).fetchone() is None:
                    return False
                if _unstarted_edges(connection, market, next_day - timedelta(days=365)) is None:
                    return False
        return True

    def pending_partitions(self, partition: DisclosurePartition) -> tuple[DisclosurePartition, ...]:
        with self._connect() as connection:
            rows = connection.run("SELECT start_date,end_date,next_page,expected_rows,received_rows FROM partitions "
                                  "WHERE market=? AND start_date>=? AND end_date<=? AND is_complete=0 ORDER BY end_date DESC,start_date", _key(partition))
            return tuple(DisclosurePartition(partition.market, date.fromisoformat(row[0]), date.fromisoformat(row[1]), row[2], row[3], row[4]) for row in rows)

    def _check_cancelled(self) -> None:
        if self._io_budget is not None:
            self._io_budget.check()
        if self._cancel_requested is not None and self._cancel_requested():
            raise InterruptedError("company_sync_cancelled")

    @contextmanager
    def _connect(self, *, accepted_document: bool = False) -> Iterator[SqliteConnection]:
        if not accepted_document:
            self._check_cancelled()
        try:
            with self._session(
                self.path,
                SqliteConnectionOptions(foreign_keys=True),
                cancel_requested=None if accepted_document else self._cancel_requested, sqlite_policy=self._sqlite_policy,
            ) as connection:
                yield connection
        except sqlite3.Error as exc:
            if str(exc) == "interrupted" and self._cancel_requested is not None and self._cancel_requested():
                raise InterruptedError("company_sync_cancelled") from exc
            raise RuntimeError("company_evidence_persistence_unavailable") from exc

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
        return self._commit_page(partition, page, observed_at, reset_invalid_cursor=True)

    def recover_directory_page(
        self,
        partition: DisclosurePartition,
        page: DisclosurePage,
        observed_at: datetime,
        state_hash: str,
    ) -> bool:
        """Restore the exact unsettled page, including acceptance before its journal acknowledgement."""
        if partition.query is not None:
            return False
        with self._connect() as connection:
            row = connection.run("""SELECT next_page,expected_rows,received_rows,is_complete,checked_at FROM partitions
                WHERE market=? AND start_date=? AND end_date=?
                AND NOT EXISTS(SELECT 1 FROM disclosure_queries q WHERE q.market=partitions.market
                    AND q.start_date=partitions.start_date AND q.end_date=partitions.end_date)""", _key(partition)).fetchone()
            if row is None:
                return False
            next_page, expected, received, complete, checked_at = row
            if next_page == partition.page and not complete:
                prior = replace(partition, expected_rows=expected, received_rows=received)
            elif (
                next_page == partition.page + 1 and received >= len(page.rows) and expected == page.total
                and bool(complete) == (not page.has_more) and checked_at == observed_at.isoformat()
            ):
                prior = replace(
                    partition, expected_rows=None if partition.page == 1 else expected,
                    received_rows=received - len(page.rows),
                )
                if CanonicalDigest(prior) != state_hash:
                    return False
                if complete:
                    # Final acceptance drops its temporary page membership.
                    # The exact coverage record and retained source identities
                    # then prove this transaction under the company write lock.
                    if connection.run(
                        "SELECT 1 FROM coverage WHERE market=? AND start_date=? AND end_date=? AND checked_at=?",
                        (*_key(partition), observed_at.isoformat()),
                    ).fetchone() is None:
                        return False
                    return all(
                        connection.run("""SELECT 1 FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id
                            WHERE d.source='cninfo' AND d.source_key=? AND c.code=? AND d.record_hash=?""",
                            (item.identity, code, item.digest)).fetchone() is not None
                        for item in page.rows for code in item.codes
                    )
                return all(
                    connection.run("""SELECT 1 FROM partition_rows p JOIN disclosures d ON d.source='cninfo' AND d.source_key=p.source_key
                        WHERE p.market=? AND p.start_date=? AND p.end_date=? AND p.source_key=? AND p.code=? AND d.record_hash=?""",
                        (*_key(partition), item.identity, code, item.digest)).fetchone() is not None
                    for item in page.rows for code in item.codes
                )
            else:
                return False
        if CanonicalDigest(prior) != state_hash:
            return False
        try:
            self._commit_page(prior, page, observed_at, reset_invalid_cursor=False)
        except CompanyInputValidationFailure:
            return False
        return True

    def _commit_page(
        self,
        partition: DisclosurePartition,
        page: DisclosurePage,
        observed_at: datetime,
        *,
        reset_invalid_cursor: bool,
    ) -> int:
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
            except CompanyInputValidationFailure:
                if reset_invalid_cursor:
                    self.restart_partition(partition)
                raise
        self._validate_page(partition, page, reset_invalid_cursor=reset_invalid_cursor)
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
            raise CompanyInputValidationFailure("cninfo_query_unsplittable")
        if len({query.key for query in queries}) != len(queries) or (
            partition.query is not None and any(query.key == partition.query.key for query in queries)
        ):
            raise CompanyInputValidationFailure("cninfo_query_split_invalid")
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

    def _validate_page(self, partition: DisclosurePartition, page: DisclosurePage, *, reset_invalid_cursor: bool = True) -> None:
        if partition.expected_rows is not None and partition.expected_rows != page.total:
            if reset_invalid_cursor:
                self.restart_partition(partition)
            raise CompanyInputValidationFailure("cninfo_partition_changed")
        received = partition.received_rows + len(page.rows)
        if not page.has_more and received != page.total:
            if reset_invalid_cursor:
                self.restart_partition(partition)
            raise CompanyInputValidationFailure("cninfo_page_truncated")
        if page.has_more and received >= page.total:
            if reset_invalid_cursor:
                self.restart_partition(partition)
            raise CompanyInputValidationFailure("cninfo_page_count_inconsistent")
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
            if reset_invalid_cursor:
                self.restart_partition(partition)
            raise CompanyInputValidationFailure("cninfo_duplicate_page_links")

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

    def document_stage(self, item: Disclosure, stage: str) -> None:
        local = self._raw_files.local(item) if self._raw_files is not None else None
        with self._connect(accepted_document=True) as connection, SqliteTransaction(connection, immediate=True):
            connection.run(
                "INSERT INTO document_tasks(source,source_key,record_hash,stage,path,payload_hash,size) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(source,source_key,record_hash) DO UPDATE SET stage=excluded.stage,"
                "path=COALESCE(excluded.path,path),payload_hash=COALESCE(excluded.payload_hash,payload_hash),size=COALESCE(excluded.size,size)",
                (item.source, item.identity, item.digest, stage, str(local.path.relative_to(self.raw_root)) if local else None,
                 local.payload_hash if local else None, local.size if local else None),
            )

    def record_failure(self, record: DownloadFailureRecord, *, configuration: DownloadFailureConfiguration) -> None:
        with self._connect(accepted_document=True) as connection, SqliteTransaction(connection, immediate=True):
            WriteDownloadFailure(connection, record, configuration)

    def recover_required_text(self, source_key: str, text: str, observed_at: datetime) -> bool:
        self._check_cancelled()
        reserve = partial(self._raw_files.reserve, self._documents.root, analysis=True) if self._raw_files is not None else None
        with self._connect() as collected, SqliteTransaction(collected, immediate=True), self._session(
            self.facts_path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy,
            cancel_requested=self._cancel_requested,
        ) as formal:
            return RecoverRequiredCompanyText(
                collected, formal, source_key, text, observed_at, self._facts.extraction_hash,
                self._documents, self._check_cancelled, reserve,
            )

    def commit_document(self, item: Disclosure, extraction: CompanyDocumentExtraction, extracted_at: datetime) -> bool:
        self._check_cancelled()
        # A result already accepted by the main thread is drained on cancellation.
        with self._connect(accepted_document=True) as connection:
            existing = connection.run(
                "SELECT id FROM disclosures WHERE source=? AND source_key=? AND record_hash=? AND body_checked=0",
                (item.source, item.identity, item.digest),
            ).fetchone()
        if existing is None:
            return False
        numbers = ExtractCompanyAnnouncementNumbers(extraction.text)
        candidate = ExtractCompanyLinkCandidate(CompanyDocument(item, extraction.text, True), extracted_at, self._event_policy)
        reserve = partial(self._raw_files.reserve, self._documents.root, analysis=True) if self._raw_files is not None else None
        # Pin the prepared file, without holding its lock across a SQLite wait.
        with (
            self._documents.prepare(extraction.text, reserve=reserve) as digest,
            self._connect(accepted_document=True) as connection,
            SqliteTransaction(connection, immediate=True),
        ):
            # A concurrent version/result change cannot acquire a reference or receipt.
            existing = connection.run(
                "SELECT id FROM disclosures WHERE source=? AND source_key=? AND record_hash=? AND body_checked=0",
                (item.source, item.identity, item.digest),
            ).fetchone()
            if existing is None:
                return False
            WriteCompanyAnnouncementNumbers(connection, existing[0], item.digest, digest, numbers)
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


def _reopen_intraday_coverage(connection: SqliteConnection, market: Market, start: date, end: date) -> None:
    # A day sampled before midnight needs a fresh, complete source enumeration.
    rows = connection.run("""SELECT c.market,c.start_date,c.end_date FROM coverage c
        JOIN partitions p USING(market,start_date,end_date) WHERE c.market=? AND c.start_date=c.end_date
        AND c.end_date BETWEEN ? AND ? AND substr(c.checked_at,1,10)=c.end_date AND p.is_complete=1""",
        (market, start.isoformat(), end.isoformat())).fetchall()
    for row in rows:
        key = (row[0], row[1], row[2])
        for table in ("partitions", "disclosure_queries"):
            connection.run(f"""UPDATE {table} SET next_page=1,expected_rows=NULL,received_rows=0,is_complete=0,failure=''
                WHERE market=? AND start_date=? AND end_date=?""", key)
        for table in ("partition_rows", "disclosure_query_rows", "partition_retries", "coverage"):
            connection.run(f"DELETE FROM {table} WHERE market=? AND start_date=? AND end_date=?", key)
    if rows:
        MarkCompanyMarketDirty(connection, market)


def _unstarted_edges(connection: SqliteConnection, market: Market, start: date) -> tuple[tuple[str, str, str], ...] | None:
    day = start.isoformat()
    keys = []
    for row in connection.run("""SELECT market,start_date,end_date,next_page,expected_rows,received_rows,failure
        FROM partitions WHERE market=? AND start_date<? AND end_date>=? AND is_complete=0""", (market, day, day)):
        key = (row[0], row[1], row[2])
        if tuple(row)[3:] != (1, None, 0, "") or any(connection.run(
            f"SELECT 1 FROM {table} WHERE market=? AND start_date=? AND end_date=? LIMIT 1", key).fetchone()
            for table in ("partition_rows", "disclosure_queries", "partition_retries", "coverage")):
            return None
        keys.append(key)
    return tuple(keys)


def _key(partition: DisclosurePartition) -> tuple[str, str, str]:
    return partition.market, partition.start.isoformat(), partition.end.isoformat()


__all__ = ["SQLiteCompanyMaterialIndex"]
