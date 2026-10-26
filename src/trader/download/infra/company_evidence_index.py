"""Single-writer incremental evidence index and transactional published read model."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview, CompanyDocumentRiskRelevant
from trader.download.domain.company_events import CompanyDataState, CompanyEventPolicy, CompanyEvidenceLink
from trader.download.domain.company_evidence import (
    CompanyDocument,
    CompanyEvidenceInput,
    CompanyFinancial,
    CompanyRatio,
    CompanyStockProgress,
    Disclosure,
    DisclosureCoverageMismatch,
    DisclosureMonths,
    DisclosurePage,
    DisclosurePartition,
    DisclosureQuery,
    DisclosureSecurity,
    DocumentRetry,
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
from trader.download.infra.company_evidence_sql import DISCLOSURE_QUERY_SCHEMA, DOCUMENT_RETRY_SCHEMA, SCHEMA
from trader.download.infra.company_facts_codec import (
    COMPANY_FACTS_SCHEMA,
    PUBLICATION_COLUMNS,
    CompanyExtractionIdentity,
    DecodeCompanyPublication,
    EncodeCompanyPublication,
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
        cancel_requested: Callable[[], bool] | None = None,
        event_policy: CompanyEventPolicy = _DEFAULT_EVENT_POLICY,
        stock_progress: Callable[[CompanyStockProgress], None] | None = None,
    ) -> None:
        self.path = path
        self._cancel_requested = cancel_requested
        self._event_policy = event_policy
        self._stock_progress = stock_progress

    def _check_cancelled(self) -> None:
        if self._cancel_requested is not None and self._cancel_requested():
            raise InterruptedError("company_sync_cancelled")

    @contextmanager
    def _connect(self) -> Iterator[SqliteConnection]:
        self._check_cancelled()
        try:
            with SqliteSession(
                self.path,
                SqliteConnectionOptions(timeout_seconds=8, foreign_keys=True),
                cancel_requested=self._cancel_requested,
            ) as connection:
                yield connection
        except sqlite3.Error as exc:
            if str(exc) == "interrupted" and self._cancel_requested is not None and self._cancel_requested():
                raise InterruptedError("company_sync_cancelled") from exc
            raise RuntimeError("company_evidence_persistence_unavailable") from exc

    def initialize(self) -> None:
        self._check_cancelled()
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
                EnsureSqliteSchema(
                    connection,
                    SCHEMA,
                    allow_create=True,
                    initialize=self._initialize_contract,
                    validate=self._validate_contract,
                )
                with SqliteTransaction(connection, immediate=True):
                    connection.run(DOCUMENT_RETRY_SCHEMA)
                    for statement in DISCLOSURE_QUERY_SCHEMA:
                        connection.run(statement)
                    ActivateDisclosureQueryPolicy(connection)
        except sqlite3.Error as exc:
            if str(exc) == "interrupted" and self._cancel_requested is not None and self._cancel_requested():
                raise InterruptedError("company_sync_cancelled") from exc
            raise RuntimeError("company_evidence_persistence_unavailable") from exc

    @staticmethod
    def _initialize_contract(connection: SqliteConnection) -> None:
        connection.run("INSERT INTO contract VALUES ('company_facts')")

    @staticmethod
    def _validate_contract(connection: SqliteConnection) -> None:
        if connection.run("SELECT 1 FROM sqlite_master WHERE type='table' AND name='contract'").fetchone() is None:
            raise ValueError("company_evidence_database_contract_mismatch")
        contract = connection.run("SELECT identity FROM contract").fetchall()
        if [row[0] for row in contract] != ["company_facts"]:
            raise ValueError("company_evidence_database_contract_mismatch")

    def partitions(self, markets: Sequence[Market], observed_at: datetime) -> tuple[DisclosurePartition, ...]:
        today = observed_at.date()
        cutoff = today - timedelta(days=365)
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            pending_before: set[tuple[str, str, str]] = set()
            for market in markets:
                pending_before.update(
                    (row["market"], row["start"], row["end"])
                    for row in connection.run(
                        "SELECT market,start,end FROM partitions WHERE market=? AND complete=0 AND end>=?",
                        (market, cutoff.isoformat()),
                    ).fetchall()
                )
                latest = connection.run(
                    "SELECT MAX(end) FROM partitions WHERE market=? AND complete=1", (market,)
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
                        "WHERE p.market=disclosure_queries.market AND p.start=disclosure_queries.start "
                        "AND p.end=disclosure_queries.end AND p.complete=1 AND ((p.end>=? AND p.checked_at<?) "
                        "OR (p.start>=? AND p.end<=? AND p.checked_at<?)))",
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
                        "UPDATE partitions SET next_page=1,expected_rows=NULL,received_rows=0,complete=0 "
                        "WHERE market=? AND complete=1 AND ((end>=? AND checked_at<?) "
                        "OR (start>=? AND end<=? AND checked_at<?))",
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
                    "WHERE p.market=disclosure_queries.market AND p.start=disclosure_queries.start AND p.end=disclosure_queries.end "
                    "AND p.complete=1 AND p.end>=? AND p.checked_at<?)",
                    (market, (today - timedelta(days=6)).isoformat(), today.isoformat()),
                )
                connection.run(
                    "UPDATE partitions SET next_page=1,expected_rows=NULL,received_rows=0,complete=0 "
                    "WHERE market=? AND complete=1 AND end>=? AND checked_at<?",
                    (market, (today - timedelta(days=6)).isoformat(), today.isoformat()),
                )
                connection.run(
                    "DELETE FROM disclosure_queries WHERE market=? AND EXISTS(SELECT 1 FROM partitions p "
                    "WHERE p.market=disclosure_queries.market AND p.start=disclosure_queries.start AND p.end=disclosure_queries.end "
                    "AND p.complete=1 AND p.end=? AND p.checked_at<?)",
                    (market, today.isoformat(), (observed_at - timedelta(minutes=15)).isoformat()),
                )
                connection.run(
                    "UPDATE partitions SET next_page=1,expected_rows=NULL,received_rows=0,complete=0 "
                    "WHERE market=? AND complete=1 AND end=? AND checked_at<?",
                    (market, today.isoformat(), (observed_at - timedelta(minutes=15)).isoformat()),
                )
            rows = connection.run(
                "SELECT * FROM partitions WHERE complete=0 AND end>=? "
                "ORDER BY CASE WHEN next_page>1 THEN 0 ELSE 1 END,end DESC,market,start",
                (cutoff.isoformat(),),
            ).fetchall()
            rows = sorted(
                rows,
                key=lambda row: (
                    row["end"] < (today - timedelta(days=6)).isoformat(),
                    (row["market"], row["start"], row["end"]) not in pending_before,
                ),
            )
        return tuple(
            DisclosurePartition(
                cast(Market, row["market"]),
                date.fromisoformat(row["start"]),
                date.fromisoformat(row["end"]),
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
                    "SELECT market,count(*) FROM partitions WHERE complete=1 AND end>=? GROUP BY market",
                    ((observed_at.date() - timedelta(days=365)).isoformat(),),
                )
                if row[0] in markets
            )

    def _add_partition(self, connection: SqliteConnection, market: str, start: date, end: date) -> None:
        # Seed only dates with no durable owner. Existing children (including old-schema
        # children) cover their parent's range, so restart/audit cannot resurrect it.
        ranges = connection.run(
            "SELECT start,end FROM partitions WHERE market=? AND start<=? AND end>=? ORDER BY start,end",
            (market, end.isoformat(), start.isoformat()),
        ).fetchall()
        cursor = start
        for row in ranges:
            left, right = date.fromisoformat(row[0]), date.fromisoformat(row[1])
            if left > cursor:
                connection.run(
                    "INSERT OR IGNORE INTO partitions(market,start,end) VALUES(?,?,?)",
                    (market, cursor.isoformat(), (left - timedelta(days=1)).isoformat()),
                )
            cursor = max(cursor, right + timedelta(days=1))
        if cursor <= end:
            connection.run(
                "INSERT OR IGNORE INTO partitions(market,start,end) VALUES(?,?,?)",
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
            connection.run("DELETE FROM partitions WHERE market=? AND start=? AND end=?", _key(partition))
            for item in children:
                self._add_partition(connection, item.market, item.start, item.end)
                connection.run(
                    "UPDATE partitions SET next_page=1,expected_rows=NULL,received_rows=0,complete=0 "
                    "WHERE market=? AND start>=? AND end<=? AND complete=1 AND checked_at<?",
                    (*_key(item), observed_at.date().isoformat()),
                )
            connection.run("DELETE FROM partition_rows WHERE market=? AND start=? AND end=?", _key(partition))
            connection.run("DELETE FROM coverage WHERE market=? AND start=? AND end=?", _key(partition))
            rows = connection.run(
                "SELECT * FROM partitions WHERE market=? AND start>=? AND end<=? AND complete=0 ORDER BY start",
                _key(partition),
            ).fetchall()
        return tuple(
            DisclosurePartition(
                partition.market,
                date.fromisoformat(row["start"]),
                date.fromisoformat(row["end"]),
                row["next_page"],
                row["expected_rows"],
                row["received_rows"],
            )
            for row in rows
        )

    def commit_page(self, partition: DisclosurePartition, page: DisclosurePage, observed_at: datetime) -> int:
        if partition.query is not None:
            try:
                with self._connect() as connection, SqliteTransaction(connection, immediate=True):
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
            if partition.page == 1:
                connection.run("DELETE FROM partition_rows WHERE market=? AND start=? AND end=?", _key(partition))
            changes = self._write_disclosures(connection, page.rows)
            for item in page.rows:
                for code in item.codes:
                    connection.run(
                        "INSERT OR IGNORE INTO partition_rows VALUES(?,?,?,?,?)",
                        (*_key(partition), item.identity, code),
                    )
            connection.run(
                "UPDATE partitions SET next_page=?,expected_rows=?,received_rows=?,complete=?,checked_at=?,failure='' "
                "WHERE market=? AND start=? AND end=?",
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
                "DELETE FROM partition_retries WHERE market=? AND start=? AND end=? AND scope=?",
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
            "AND NOT EXISTS(SELECT 1 FROM partition_rows p WHERE p.market=? AND p.start=? AND p.end=? "
            "AND p.source_id=d.source_id AND p.code=c.code)",
            (partition.start.isoformat(), (partition.end + timedelta(days=1)).isoformat(), *_key(partition)),
        ).fetchall()
        for row in stale:
            if EvidenceMarket(row["code"]) == partition.market:
                connection.run("DELETE FROM disclosure_codes WHERE disclosure_id=? AND code=?", tuple(row))

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
                    "UPDATE partitions SET complete=1,next_page=1,expected_rows=?,received_rows=?,checked_at=?,failure='' "
                    "WHERE market=? AND start=? AND end=?",
                    (page.total, page.total, observed_at.isoformat(), *_key(partition)),
                )
                self._finish_partition(connection, partition, observed_at)
                connection.run("DELETE FROM partition_retries WHERE market=? AND start=? AND end=?", _key(partition))
        except DisclosureCoverageMismatch as exc:
            if exc.refresh_required:
                # A smaller source set or changed sample really invalidates the
                # saved generation. Preserve its historical issuer membership.
                with self._connect() as connection, SqliteTransaction(connection, immediate=True):
                    connection.run(
                        "UPDATE disclosure_queries SET next_page=1,expected_rows=NULL,received_rows=0,complete=0 "
                        "WHERE market=? AND start=? AND end=?",
                        _key(partition),
                    )
                    connection.run(
                        "DELETE FROM disclosure_query_rows WHERE market=? AND start=? AND end=?", _key(partition)
                    )
            raise

    def partition_retry_after(self, partition: DisclosurePartition) -> datetime | None:
        with self._connect() as connection:
            row = connection.run(
                "SELECT retry_after FROM partition_retries WHERE market=? AND start=? AND end=? AND scope=?",
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
                        "SELECT 1 FROM partition_rows WHERE market=? AND start=? AND end=? AND source_id=? AND code=?",
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
                "UPDATE partitions SET next_page=1,expected_rows=NULL,received_rows=0,complete=0 "
                "WHERE market=? AND start=? AND end=?",
                _key(partition),
            )

    def partition_failure(self, partition: DisclosurePartition, failure: str) -> None:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            if partition.query is not None:
                connection.run(
                    "UPDATE disclosure_queries SET failure=? WHERE market=? AND start=? AND end=? AND scope=?",
                    (failure, *DisclosureQueryKey(partition)),
                )
            connection.run(
                "UPDATE partitions SET failure=? WHERE market=? AND start=? AND end=?", (failure, *_key(partition))
            )

    def _write_disclosures(self, connection: SqliteConnection, rows: Sequence[Disclosure]) -> int:
        changes = connection.run_many(
            "INSERT INTO disclosures(source,source_id,kind,title,published_at,url,digest) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(source,source_id) DO UPDATE SET title=excluded.title,published_at=excluded.published_at,"
            "url=excluded.url,digest=excluded.digest,body='',body_checked=0 WHERE digest<>excluded.digest",
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
        connection.run_many(
            "INSERT OR IGNORE INTO disclosure_codes(code,disclosure_id) "
            "SELECT ?,id FROM disclosures WHERE source=? AND source_id=?",
            ((code, item.source, item.identity) for item in rows for code in item.codes),
        )
        return changes

    def pending_documents(self, codes: Sequence[str], observed_at: datetime) -> tuple[Disclosure, ...]:
        with self._connect() as connection:
            rows = connection.run(
                "SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                "WHERE kind='announcement' AND body_checked=0 AND published_at>=? AND published_at<=? "
                "ORDER BY published_at DESC",
                ((observed_at.date() - timedelta(days=365)).isoformat(), observed_at.isoformat()),
            ).fetchall()
        allowed = frozenset(codes)
        return tuple(
            _disclosure(row) for row in rows if row["code"] in allowed and CompanyDocumentNeedsReview(row["title"])
        )

    def commit_document(self, item: Disclosure, text: str) -> None:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            connection.run(
                "UPDATE disclosures SET body=?,body_checked=1 WHERE source=? AND source_id=? AND digest=?",
                (text, item.source, item.identity, item.digest),
            )
            connection.run(
                "DELETE FROM document_retries WHERE disclosure_id IN "
                "(SELECT id FROM disclosures WHERE source=? AND source_id=? AND digest=?)",
                (item.source, item.identity, item.digest),
            )

    def document_retry(self, item: Disclosure, policy: str) -> DocumentRetry | None:
        with self._connect() as connection:
            row = connection.run(
                "SELECT r.failure,r.retry_after FROM document_retries r JOIN disclosures d ON d.id=r.disclosure_id "
                "WHERE d.source=? AND d.source_id=? AND d.digest=? AND r.digest=d.digest AND r.policy=?",
                (item.source, item.identity, item.digest, policy),
            ).fetchone()
        return DocumentRetry(row[0], datetime.fromisoformat(row[1])) if row else None

    def document_failure(
        self, item: Disclosure, policy: str, failure: str, attempted_at: datetime, retry_after: datetime
    ) -> None:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            connection.run(
                "INSERT INTO document_retries(disclosure_id,digest,policy,failure,attempted_at,retry_after,attempts) "
                "SELECT id,digest,?,?,?,?,1 FROM disclosures WHERE source=? AND source_id=? AND digest=? "
                "ON CONFLICT(disclosure_id) DO UPDATE SET digest=excluded.digest,policy=excluded.policy,failure=excluded.failure,"
                "attempted_at=excluded.attempted_at,retry_after=excluded.retry_after,"
                "attempts=CASE WHEN document_retries.digest=excluded.digest THEN document_retries.attempts+1 ELSE 1 END",
                (
                    policy,
                    failure,
                    attempted_at.isoformat(),
                    retry_after.isoformat(),
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
                "SELECT start,end,next_page,expected_rows,received_rows FROM structured_progress WHERE component=? AND scope='market'",
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
                        f"SELECT code,day FROM structured_members WHERE component=? AND code IN ({placeholders})",
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
                "published_at=excluded.published_at,basic_eps=excluded.basic_eps,book_value_per_share=excluded.book_value_per_share,"
                "revenue_growth_pct=excluded.revenue_growth_pct,net_profit_growth_pct=excluded.net_profit_growth_pct,"
                "core_profit_growth_pct=excluded.core_profit_growth_pct,roe_pct=excluded.roe_pct,"
                "parent_net_profit=excluded.parent_net_profit,core_net_profit=excluded.core_net_profit "
                "WHERE (published_at,basic_eps,book_value_per_share,revenue_growth_pct,net_profit_growth_pct,"
                "core_profit_growth_pct,roe_pct,parent_net_profit,core_net_profit) IS NOT "
                "(excluded.published_at,excluded.basic_eps,excluded.book_value_per_share,excluded.revenue_growth_pct,"
                "excluded.net_profit_growth_pct,excluded.core_profit_growth_pct,excluded.roe_pct,"
                "excluded.parent_net_profit,excluded.core_net_profit)",
                financial_values,
            )
            changes = connection.total_changes - before
            if page.page == page.pages:
                if page.component != "financial":
                    before = connection.total_changes
                    connection.run(
                        "DELETE FROM ratios WHERE component=? AND NOT EXISTS(SELECT 1 FROM structured_members m "
                        "WHERE m.component=ratios.component AND m.code=ratios.code AND m.day=ratios.day)",
                        (page.component,),
                    )
                    connection.run(
                        "INSERT INTO ratios SELECT component,code,day,ratio_pct FROM structured_members WHERE component=? "
                        "ON CONFLICT(component,code,day) DO UPDATE SET ratio_pct=excluded.ratio_pct WHERE ratio_pct<>excluded.ratio_pct",
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

    def commit_news(self, code: str, rows: Sequence[Disclosure], observed_at: datetime) -> int:
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            changes = self._write_disclosures(connection, rows)
            connection.run(
                "INSERT OR REPLACE INTO component_checks VALUES('news',?,?,'')", (code, observed_at.isoformat())
            )
        return changes

    def publish(self, codes: Sequence[str], observed_at: datetime) -> int:
        requested = tuple(sorted(set(codes)))
        if len(requested) > 12_000:
            raise ValueError("company_publication_population_exceeded")
        # TEMP belongs to this connection and disappears on close. It is never a second publication.
        with (
            self._connect() as connection,
            SqliteSession(
                self.path,
                SqliteConnectionOptions(read_only=True, timeout_seconds=8),
                cancel_requested=self._cancel_requested,
            ) as source,
        ):
            connection.run("PRAGMA temp_store=FILE")
            connection.run("CREATE TEMP TABLE prepared_publication AS SELECT * FROM published WHERE 0")
            source_version = source.run("PRAGMA data_version").require_one()[0]
            digest = hashlib.sha256()
            extraction_identity = CompanyExtractionIdentity(self._event_policy)
            digest.update(extraction_identity.encode())
            columns = ",".join(PUBLICATION_COLUMNS)
            placeholders = ",".join("?" for _ in PUBLICATION_COLUMNS)
            with SqliteTransaction(source):
                shared = _snapshot_shared(source, observed_at)
                for offset in range(0, len(requested), 128):
                    batch = requested[offset : offset + 128]
                    inputs = _snapshot_rows(source, batch, observed_at)
                    previous = {
                        str(row["code"]): DecodeCompanyPublication(dict(row))
                        for row in source.run(
                            f"SELECT {columns} FROM published WHERE code IN ({','.join('?' for _ in batch)})", batch
                        )
                    }
                    values = []
                    for code in batch:
                        self._check_cancelled()
                        snapshot = _build_snapshot(code, observed_at, inputs[code], shared)
                        facts = CompileCompanyFacts(snapshot, self._event_policy, previous=previous.get(code))
                        prior = previous.get(code)
                        if prior is not None and replace(facts, published_at=prior.published_at) == prior:
                            facts = prior
                        row = EncodeCompanyPublication(facts)
                        values.append(row)
                        if self._stock_progress is not None:
                            self._stock_progress(
                                CompanyStockProgress(
                                    code,
                                    processed=offset + len(values),
                                    total=len(requested),
                                    announcements=len(snapshot.announcements),
                                    analyzed=sum(item.checked for item in snapshot.announcements),
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
                    with SqliteTransaction(connection):
                        connection.run_many(f"INSERT INTO temp.prepared_publication VALUES({placeholders})", values)
            self._check_cancelled()
            with SqliteTransaction(connection, immediate=True):
                # This same read handle observes every concurrent commit, including other publications.
                if source.run("PRAGMA data_version").require_one()[0] != source_version:
                    raise RuntimeError("company_publication_source_changed")
                assignments = ",".join(
                    f"{column}=excluded.{column}" for column in PUBLICATION_COLUMNS if column != "code"
                )
                changes = " OR ".join(
                    f"published.{column} IS NOT excluded.{column}" for column in PUBLICATION_COLUMNS if column != "code"
                )
                connection.run(
                    f"INSERT INTO published SELECT * FROM temp.prepared_publication WHERE true ON CONFLICT(code) DO UPDATE SET {assignments} WHERE {changes}"
                )
                for published_row in connection.run(f"SELECT {columns} FROM published ORDER BY code"):
                    digest.update(repr(tuple(published_row)).encode())
                connection.run(
                    "INSERT OR REPLACE INTO publication VALUES(1,?,?,?,?)",
                    (digest.hexdigest(), observed_at.isoformat(), COMPANY_FACTS_SCHEMA, extraction_identity),
                )
        self._prune(observed_at)
        return len(requested)

    def _prune(self, observed_at: datetime) -> None:
        # Cleanup is independent of the short publication transaction; failure cannot roll back its pointer.
        with self._connect() as connection, SqliteTransaction(connection, immediate=True):
            cutoff = (observed_at.date() - timedelta(days=365)).isoformat()
            connection.run(
                "DELETE FROM disclosures WHERE ((kind='announcement' AND published_at<?) OR (kind='news' AND published_at<?)) "
                "AND source_id NOT IN (SELECT ref.value FROM published p, json_each(p.risk_events_json) e, "
                "json_each(e.value,'$.evidence_ids') ref UNION SELECT ref.value FROM published p, "
                "json_each(p.positive_events_json) e, json_each(e.value,'$.evidence_ids') ref)",
                (cutoff, (observed_at - timedelta(days=7)).isoformat()),
            )
            connection.run("DELETE FROM partitions WHERE end<?", (cutoff,))
            connection.run("DELETE FROM coverage WHERE end<?", (cutoff,))
            connection.run("DELETE FROM partition_rows WHERE end<?", (cutoff,))
            connection.run(
                "DELETE FROM financials WHERE report_date<?", ((observed_at.date() - timedelta(days=550)).isoformat(),)
            )


def _key(partition: DisclosurePartition) -> tuple[str, str, str]:
    return partition.market, partition.start.isoformat(), partition.end.isoformat()


def _disclosure(row: sqlite3.Row) -> Disclosure:
    return Disclosure(
        row["source"],
        row["source_id"],
        (row["code"],),
        row["title"],
        datetime.fromisoformat(row["published_at"]),
        row["url"],
        row["digest"],
        cast(Literal["announcement", "news"], row["kind"]),
    )


@dataclass(frozen=True)
class _SnapshotRows:
    disclosures: tuple[sqlite3.Row, ...]
    financials: tuple[sqlite3.Row, ...]
    ratios: tuple[sqlite3.Row, ...]
    checks: tuple[sqlite3.Row, ...]


@dataclass(frozen=True)
class _SnapshotShared:
    coverage: tuple[sqlite3.Row, ...]
    checks: tuple[sqlite3.Row, ...]
    errors: tuple[sqlite3.Row, ...]
    pledge_day: str | None
    unlock_snapshot: sqlite3.Row | None


def _snapshot_shared(connection: SqliteConnection, now: datetime) -> _SnapshotShared:
    cutoff = (now.date() - timedelta(days=365)).isoformat()
    pledge = connection.run("SELECT start FROM ratio_snapshots WHERE component='pledge'").fetchone()
    return _SnapshotShared(
        tuple(
            connection.run("SELECT start,end,market,checked_at FROM coverage WHERE end>=? ORDER BY start", (cutoff,))
        ),
        tuple(connection.run("SELECT component,checked_at,failure FROM component_checks WHERE scope='market'")),
        tuple(
            connection.run(
                "SELECT DISTINCT CASE WHEN failure='' THEN 'pending' ELSE failure END AS failure,market "
                "FROM partitions WHERE end>=? AND (failure<>'' OR complete=0)",
                (cutoff,),
            )
        ),
        pledge[0] if pledge else None,
        connection.run("SELECT checked_at,end FROM ratio_snapshots WHERE component='unlock'").fetchone(),
    )


def _snapshot_rows(connection: SqliteConnection, codes: tuple[str, ...], now: datetime) -> dict[str, _SnapshotRows]:
    placeholders = ",".join("?" for _ in codes)
    disclosures = connection.run(
        "SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
        f"WHERE c.code IN ({placeholders}) AND d.published_at>=? AND d.published_at<=? ORDER BY d.published_at,d.id",
        (*codes, (now.date() - timedelta(days=365)).isoformat(), now.isoformat()),
    )
    documents: dict[str, list[sqlite3.Row]] = {code: [] for code in codes}
    for row in disclosures:
        documents[row["code"]].append(row)
    financials: dict[str, list[sqlite3.Row]] = {code: [] for code in codes}
    for row in connection.run(
        f"SELECT * FROM financials WHERE code IN ({placeholders}) AND report_date>=? AND published_at<=? ORDER BY report_date",
        (*codes, (now.date() - timedelta(days=550)).isoformat(), now.isoformat()),
    ):
        financials[row["code"]].append(row)
    ratios: dict[str, list[sqlite3.Row]] = {code: [] for code in codes}
    for row in connection.run(
        f"SELECT code,day,ratio_pct,component FROM ratios WHERE code IN ({placeholders}) ORDER BY day", codes
    ):
        ratios[row["code"]].append(row)
    checks: dict[str, list[sqlite3.Row]] = {code: [] for code in codes}
    for row in connection.run(
        f"SELECT component,checked_at,failure,scope FROM component_checks WHERE scope IN ({placeholders})", codes
    ):
        checks[row["scope"]].append(row)
    return {
        code: _SnapshotRows(tuple(documents[code]), tuple(financials[code]), tuple(ratios[code]), tuple(checks[code]))
        for code in codes
    }


def _build_snapshot(code: str, now: datetime, inputs: _SnapshotRows, shared: _SnapshotShared) -> CompanyEvidenceInput:
    start, end = now.date() - timedelta(days=365), now.date()
    rows = inputs.disclosures
    documents = tuple(
        CompanyDocument(_disclosure(row), row["body"], bool(row["body_checked"]))
        for row in rows
        if row["kind"] == "announcement"
    )
    ranges = tuple(row for row in shared.coverage if row["market"] == EvidenceMarket(code))
    frontier = start
    for row in ranges:
        if date.fromisoformat(row[0]) <= frontier:
            frontier = max(frontier, date.fromisoformat(row[1]) + timedelta(days=1))
    complete = frontier > end and all(
        item.checked or not CompanyDocumentRiskRelevant(item.disclosure.title) for item in documents
    )
    financials = tuple(
        CompanyFinancial(row[0], date.fromisoformat(row[1]), datetime.fromisoformat(row[2]), *tuple(row)[3:])
        for row in inputs.financials
    )
    pledge_rows = tuple(row for row in inputs.ratios if row["component"] == "pledge" and row["day"] <= end.isoformat())
    pledge_row = pledge_rows[-1] if pledge_rows else None
    pledge = CompanyRatio(pledge_row[0], date.fromisoformat(pledge_row[1]), pledge_row[2]) if pledge_row else None
    if pledge is None and shared.pledge_day:
        pledge = CompanyRatio(code, date.fromisoformat(shared.pledge_day), 0.0)
    unlocks = tuple(
        CompanyRatio(row[0], date.fromisoformat(row[1]), row[2])
        for row in inputs.ratios
        if row["component"] == "unlock" and end.isoformat() <= row["day"] <= (end + timedelta(days=180)).isoformat()
    )
    checks = (*shared.checks, *inputs.checks)
    failures = tuple(f"{row[0]}:{row[2]}" for row in checks if row[2])
    for component in ("financial", "pledge", "unlock"):
        if not any(
            row[0] == component and row[1] and datetime.fromisoformat(row[1]) >= now - timedelta(days=1) and not row[2]
            for row in checks
        ):
            failures += (f"{component}:not_current",)
    if pledge is not None and pledge.day < end - timedelta(days=10):
        failures += ("pledge:source_stale",)
    partition_errors = tuple(row for row in shared.errors if row["market"] == EvidenceMarket(code))
    failures += tuple(f"announcements:{row[0]}" for row in partition_errors)
    complete = complete and not partition_errors
    if not complete:
        failures += ("announcements:window_incomplete",)
    news = tuple(
        _disclosure(row)
        for row in rows
        if row["kind"] == "news" and datetime.fromisoformat(row["published_at"]) >= now - timedelta(days=7)
    )
    snapshot = CompanyEvidenceInput(
        code,
        now,
        start,
        end,
        "",
        documents,
        complete,
        financials,
        pledge,
        unlocks,
        not any(item.startswith("unlock:") for item in failures),
        news,
        any(
            row[0] == "news" and row[1] and datetime.fromisoformat(row[1]) >= now - timedelta(days=1) and not row[2]
            for row in checks
        ),
        failures,
        source_checked_at=max(
            (
                datetime.fromisoformat(value)
                for value in (*(row["checked_at"] for row in ranges), *(row[1] for row in checks))
                if value
            ),
            default=None,
        ),
        unlock_checked_at=datetime.fromisoformat(shared.unlock_snapshot[0]) if shared.unlock_snapshot else None,
        unlock_coverage_end=date.fromisoformat(shared.unlock_snapshot[1]) if shared.unlock_snapshot else None,
    )
    return snapshot


class SQLiteCompanyEvidenceReader:
    """Only the published, indexed projection is visible to recommendation."""

    def __init__(self, path: Path, *, event_policy: CompanyEventPolicy = _DEFAULT_EVENT_POLICY) -> None:
        self.path = path
        self._extraction_identity = CompanyExtractionIdentity(event_policy)

    def _publication_version(self, connection: SqliteConnection) -> str:
        row = connection.run(
            "SELECT version,schema_identity,extraction_identity FROM publication WHERE singleton=1"
        ).fetchone()
        if row is None:
            return ""
        if row[1] != COMPANY_FACTS_SCHEMA or row[2] != self._extraction_identity:
            raise ValueError("company_publication_identity_mismatch")
        return str(row[0])

    def document_links(self, code: str, evidence_ids: tuple[str, ...]) -> tuple[CompanyEvidenceLink, ...]:
        if not evidence_ids or not self.path.exists():
            return ()
        if len(evidence_ids) > 32 or len(code) != 6 or not code.isascii() or not code.isdigit():
            raise ValueError("company_document_query_invalid")
        try:
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True, timeout_seconds=2)) as connection:
                rows = connection.run(
                    "SELECT d.source_id,d.title,d.url,d.published_at,d.body_checked FROM disclosures d "
                    "JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE c.code=? AND d.kind='announcement' "
                    f"AND d.source_id IN ({','.join('?' for _ in evidence_ids)}) ORDER BY d.published_at DESC LIMIT 32",
                    (code, *evidence_ids),
                ).fetchall()
                return tuple(
                    CompanyEvidenceLink(row[0], row[1], row[2], datetime.fromisoformat(row[3]), bool(row[4]))
                    for row in rows
                )
        except (sqlite3.Error, ValueError) as exc:
            raise RuntimeError("company_evidence_links_unavailable") from exc

    def read(self, codes: Sequence[str]) -> tuple[CompanyFacts, ...]:
        snapshots: list[CompanyFacts] = []
        self.visit(codes, snapshots.append)
        return tuple(snapshots)

    def publication_version(self) -> str:
        if not self.path.exists():
            return ""
        try:
            with SqliteSession(self.path, SqliteConnectionOptions(read_only=True, timeout_seconds=2)) as connection:
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
        if not self.path.exists():
            return
        captured: list[CompanyFacts] = []
        try:
            with SqliteSession(
                self.path, SqliteConnectionOptions(read_only=True, timeout_seconds=2), cancel_requested=cancel_requested
            ) as connection:
                with SqliteTransaction(connection):
                    contract = connection.run("SELECT identity FROM contract").fetchall()
                    if [tuple(row) for row in contract] != [("company_facts",)]:
                        raise ValueError("company_snapshot_contract_mismatch")
                    if not self._publication_version(connection):
                        return
                    for offset in range(0, len(codes), 512):
                        if cancel_requested is not None and cancel_requested():
                            raise InterruptedError("company_preload_cancelled")
                        batch = tuple(codes[offset : offset + 512])
                        placeholders = ",".join("?" for _ in batch)
                        captured.extend(
                            DecodeCompanyPublication(dict(row))
                            for row in connection.run(
                                f"SELECT {','.join(PUBLICATION_COLUMNS)} FROM published WHERE code IN ({placeholders})",
                                batch,
                            ).fetchall()
                        )
        except sqlite3.Error as exc:
            if cancel_requested is not None and cancel_requested():
                raise InterruptedError("company_preload_cancelled") from exc
            raise RuntimeError("company_snapshot_unreadable") from exc
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


__all__ = ["SQLiteCompanyEvidenceIndex", "SQLiteCompanyEvidenceReader"]
