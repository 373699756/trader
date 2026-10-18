"""Single-writer incremental evidence index and transactional published read model."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from trader.download.domain.company_evidence import (
    CompanyDocument,
    CompanyEvidenceSnapshot,
    CompanyFinancial,
    CompanyRatio,
    Disclosure,
    DisclosureMonths,
    DisclosurePage,
    DisclosurePartition,
    EvidenceMarket,
    Market,
    StructuredCompanyPage,
    StructuredCompanyQuery,
)
from trader.download.infra.company_evidence_codec import (
    CompanySnapshotDigest,
    DecodeCompanySnapshot,
    EncodeCompanySnapshot,
)
from trader.download.infra.company_evidence_sql import SCHEMA


class SQLiteCompanyEvidenceIndex:
    """Own writes to the fresh database; all connections have bounded lifetimes."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=8.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        except sqlite3.Error as exc:
            raise RuntimeError("company_evidence_persistence_unavailable") from exc
        finally:
            if connection is not None:
                connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "contract" not in tables:
                raise ValueError("company_evidence_database_contract_mismatch")
            if "contract" in tables:
                contract = connection.execute("SELECT identity FROM contract").fetchall()
                if [row[0] for row in contract] != ["company_evidence"]:
                    raise ValueError("company_evidence_database_contract_mismatch")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)

    def partitions(self, markets: Sequence[Market], observed_at: datetime) -> tuple[DisclosurePartition, ...]:
        today = observed_at.date()
        cutoff = today - timedelta(days=365)
        with self._connect() as connection:
            for market in markets:
                latest = connection.execute(
                    "SELECT MAX(end) FROM partitions WHERE market=? AND complete=1", (market,)
                ).fetchone()[0]
                baseline_start = max(cutoff, date.fromisoformat(latest) + timedelta(days=1)) if latest else cutoff
                for start, end in DisclosureMonths(baseline_start, today):
                    self._add_partition(connection, market, start, end)
                # Check each recent day separately: fixed pages survive interruption and don't shift at midnight.
                for offset in range(7):
                    day = today - timedelta(days=offset)
                    self._add_partition(connection, market, day, day)
                # One old month per day, excluding the recent overlap. Bound supplier correction audits.
                months = DisclosureMonths(cutoff, today - timedelta(days=7))
                if months:
                    start, end = months[today.toordinal() % len(months)]
                    self._add_partition(connection, market, start, end)
                connection.execute(
                    "UPDATE partitions SET next_page=1,expected_rows=NULL,received_rows=0,complete=0 "
                    "WHERE market=? AND complete=1 AND ((start>=? AND checked_at<?) "
                    "OR (start=? AND end=? AND checked_at<?))",
                    (
                        market,
                        (today - timedelta(days=6)).isoformat(),
                        (observed_at - timedelta(minutes=15)).isoformat(),
                        start.isoformat(),
                        end.isoformat(),
                        today.isoformat(),
                    ),
                )
            rows = connection.execute(
                "SELECT * FROM partitions WHERE complete=0 AND end>=? ORDER BY end DESC,market,start",
                (cutoff.isoformat(),),
            ).fetchall()
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

    def _add_partition(self, connection: sqlite3.Connection, market: str, start: date, end: date) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO partitions(market,start,end) VALUES(?,?,?)",
            (market, start.isoformat(), end.isoformat()),
        )

    def split_partition(self, partition: DisclosurePartition, observed_at: datetime) -> tuple[DisclosurePartition, ...]:
        if partition.start == partition.end:
            raise ValueError("cninfo_single_day_page_limit")
        middle = partition.start + (partition.end - partition.start) // 2
        children = (
            replace(partition, end=middle, page=1, expected_rows=None, received_rows=0),
            replace(partition, start=middle + timedelta(days=1), page=1, expected_rows=None, received_rows=0),
        )
        with self._connect() as connection:
            for item in children:
                self._add_partition(connection, item.market, item.start, item.end)
                connection.execute(
                    "UPDATE partitions SET next_page=1,expected_rows=NULL,received_rows=0,complete=0 "
                    "WHERE market=? AND start=? AND end=? AND complete=1 AND checked_at<?",
                    (*_key(item), observed_at.date().isoformat()),
                )
            connection.execute("DELETE FROM partitions WHERE market=? AND start=? AND end=?", _key(partition))
            rows = tuple(
                connection.execute(
                    "SELECT * FROM partitions WHERE market=? AND start=? AND end=?", _key(item)
                ).fetchone()
                for item in children
            )
        return tuple(
            replace(item, page=row["next_page"], expected_rows=row["expected_rows"], received_rows=row["received_rows"])
            for item, row in zip(children, rows, strict=True)
            if not row["complete"]
        )

    def commit_page(self, partition: DisclosurePartition, page: DisclosurePage, observed_at: datetime) -> int:
        self._validate_page(partition, page)
        received = partition.received_rows + len(page.rows)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if partition.page == 1:
                connection.execute("DELETE FROM partition_rows WHERE market=? AND start=? AND end=?", _key(partition))
            changes = self._write_disclosures(connection, page.rows)
            for item in page.rows:
                for code in item.codes:
                    connection.execute(
                        "INSERT OR IGNORE INTO partition_rows VALUES(?,?,?,?,?)",
                        (*_key(partition), item.identity, code),
                    )
            connection.execute(
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
            # Removed/corrected listings are recognized only after a complete date slice, never after a failed page.
            if not page.has_more:
                connection.execute(
                    "INSERT OR REPLACE INTO coverage VALUES(?,?,?,?)", (*_key(partition), observed_at.isoformat())
                )
                stale = connection.execute(
                    "SELECT d.id,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                    "WHERE d.source='cninfo' AND substr(d.published_at,1,10) BETWEEN ? AND ? "
                    "AND NOT EXISTS(SELECT 1 FROM partition_rows p WHERE p.market=? AND p.start=? AND p.end=? "
                    "AND p.source_id=d.source_id AND p.code=c.code)",
                    (partition.start.isoformat(), partition.end.isoformat(), *_key(partition)),
                ).fetchall()
                for row in stale:
                    if EvidenceMarket(row["code"]) == partition.market:
                        connection.execute("DELETE FROM disclosure_codes WHERE disclosure_id=? AND code=?", tuple(row))
        return changes

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
                    connection.execute(
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
        with self._connect() as connection:
            connection.execute(
                "UPDATE partitions SET next_page=1,expected_rows=NULL,received_rows=0,complete=0 "
                "WHERE market=? AND start=? AND end=?",
                _key(partition),
            )

    def partition_failure(self, partition: DisclosurePartition, failure: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE partitions SET failure=? WHERE market=? AND start=? AND end=?", (failure, *_key(partition))
            )

    def _write_disclosures(self, connection: sqlite3.Connection, rows: Sequence[Disclosure]) -> int:
        changes = 0
        for item in rows:
            cursor = connection.execute(
                "INSERT INTO disclosures(source,source_id,kind,title,published_at,url,digest) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(source,source_id) DO UPDATE SET title=excluded.title,published_at=excluded.published_at,"
                "url=excluded.url,digest=excluded.digest,body='',body_checked=0 WHERE digest<>excluded.digest",
                (
                    item.source,
                    item.identity,
                    item.kind,
                    item.title,
                    item.published_at.isoformat(),
                    item.url,
                    item.digest,
                ),
            )
            changes += cursor.rowcount
            identity = connection.execute(
                "SELECT id FROM disclosures WHERE source=? AND source_id=?", (item.source, item.identity)
            ).fetchone()[0]
            connection.executemany(
                "INSERT OR IGNORE INTO disclosure_codes VALUES(?,?)", ((code, identity) for code in item.codes)
            )
        return changes

    def pending_documents(self, codes: Sequence[str], observed_at: datetime) -> tuple[Disclosure, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
                "WHERE kind='announcement' AND body_checked=0 AND published_at>=? ORDER BY published_at DESC",
                ((observed_at - timedelta(days=365)).isoformat(),),
            ).fetchall()
        allowed = frozenset(codes)
        return tuple(_disclosure(row) for row in rows if row["code"] in allowed and NeedsDocumentReview(row["title"]))

    def commit_document(self, item: Disclosure, text: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE disclosures SET body=?,body_checked=1 WHERE source=? AND source_id=? AND digest=?",
                (text, item.source, item.identity, item.digest),
            )

    def component_due(self, component: str, scope: str, observed_at: datetime) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT checked_at,failure FROM component_checks WHERE component=? AND scope=?", (component, scope)
            ).fetchone()
        ttl = timedelta(minutes=15) if component in {"financial", "news"} else timedelta(hours=4)
        return row is None or row[0] is None or bool(row[1]) or datetime.fromisoformat(row[0]) <= observed_at - ttl

    def component_since(self, component: str, scope: str, observed_at: datetime) -> date:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT checked_at FROM component_checks WHERE component=? AND scope=?", (component, scope)
            ).fetchone()
        return (
            datetime.fromisoformat(row[0]).date() - timedelta(days=7)
            if row and row[0]
            else observed_at.date() - timedelta(days=550)
        )

    def structured_cursor(self, query: StructuredCompanyQuery) -> StructuredCompanyQuery:
        with self._connect() as connection:
            row = connection.execute(
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
            duplicate = len(keys) != len(set(keys)) or (
                page.page > 1
                and any(
                    connection.execute(
                        "SELECT 1 FROM structured_members WHERE component=? AND code=? AND day=?",
                        (page.component, code, day.isoformat()),
                    ).fetchone()
                    is not None
                    for code, day in keys
                )
            )
        if duplicate:
            self._restart_structured(page.component, scope)
            raise ValueError("structured_duplicate_page_rows")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if page.page == 1:
                connection.execute("DELETE FROM structured_members WHERE component=?", (page.component,))
            connection.executemany(
                "INSERT INTO structured_members VALUES(?,?,?,?)",
                ((page.component, code, day.isoformat(), ratio) for code, day, ratio in members),
            )
            before = connection.total_changes
            for item in page.financials:
                values = (
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
                old = connection.execute(
                    "SELECT * FROM financials WHERE code=? AND report_date=?", values[:2]
                ).fetchone()
                if old is None or tuple(old) != values:
                    connection.execute("INSERT OR REPLACE INTO financials VALUES(?,?,?,?,?,?,?,?,?,?,?)", values)
            changes = connection.total_changes - before
            if page.page == page.pages:
                if page.component != "financial":
                    before = connection.total_changes
                    connection.execute(
                        "DELETE FROM ratios WHERE component=? AND NOT EXISTS(SELECT 1 FROM structured_members m "
                        "WHERE m.component=ratios.component AND m.code=ratios.code AND m.day=ratios.day)",
                        (page.component,),
                    )
                    connection.execute(
                        "INSERT INTO ratios SELECT component,code,day,ratio_pct FROM structured_members WHERE component=? "
                        "ON CONFLICT(component,code,day) DO UPDATE SET ratio_pct=excluded.ratio_pct WHERE ratio_pct<>excluded.ratio_pct",
                        (page.component,),
                    )
                    changes += connection.total_changes - before
                    connection.execute(
                        "INSERT OR REPLACE INTO ratio_snapshots VALUES(?,?,?,?)",
                        (page.component, start.isoformat(), end.isoformat(), observed_at.isoformat()),
                    )
                connection.execute(
                    "DELETE FROM structured_progress WHERE component=? AND scope=?", (page.component, scope)
                )
                connection.execute(
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
                connection.execute(
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
        with self._connect() as connection:
            connection.execute("DELETE FROM structured_progress WHERE component=? AND scope=?", (component, scope))
            connection.execute("DELETE FROM structured_members WHERE component=?", (component,))

    def component_failure(self, component: str, scope: str, failure: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO component_checks(component,scope,failure) VALUES(?,?,?) "
                "ON CONFLICT(component,scope) DO UPDATE SET failure=excluded.failure",
                (component, scope, failure),
            )

    def commit_news(self, code: str, rows: Sequence[Disclosure], observed_at: datetime) -> int:
        with self._connect() as connection:
            changes = self._write_disclosures(connection, rows)
            connection.execute(
                "INSERT OR REPLACE INTO component_checks VALUES('news',?,?,'')", (code, observed_at.isoformat())
            )
        return changes

    def candidate_codes(self, observed_at: datetime) -> tuple[str, ...]:
        with self._connect() as connection:
            return tuple(
                row[0]
                for row in connection.execute(
                    "SELECT code FROM news_candidates WHERE selected_at>=? ORDER BY code",
                    ((observed_at - timedelta(days=7)).isoformat(),),
                )
            )

    def record_candidates(self, codes: Sequence[str], observed_at: datetime) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM news_candidates")
            connection.executemany(
                "INSERT OR REPLACE INTO news_candidates VALUES(?,?)",
                ((code, observed_at.isoformat()) for code in codes),
            )

    def publish(self, codes: Sequence[str], observed_at: datetime) -> int:
        # All normalized reads and the publication pointer share one transaction. No whole-file copy/merge.
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            digest = hashlib.sha256()
            for code in codes:
                snapshot = _build_snapshot(connection, code, observed_at)
                digest.update(snapshot.version.encode())
                payload = EncodeCompanySnapshot(snapshot)
                connection.execute(
                    "INSERT INTO published VALUES(?,?,?,?) ON CONFLICT(code) DO UPDATE "
                    "SET version=excluded.version,published_at=excluded.published_at,payload=excluded.payload "
                    "WHERE version<>excluded.version",
                    (snapshot.code, snapshot.version, observed_at.isoformat(), payload),
                )
            version = digest.hexdigest()
            connection.execute("INSERT OR REPLACE INTO publication VALUES(1,?,?)", (version, observed_at.isoformat()))
            # Only this fresh evidence database is pruned; frozen decisions and old databases are untouched.
            cutoff = (observed_at.date() - timedelta(days=365)).isoformat()
            connection.execute(
                "DELETE FROM disclosures WHERE (kind='announcement' AND published_at<?) OR (kind='news' AND published_at<?)",
                (cutoff, (observed_at - timedelta(days=7)).isoformat()),
            )
            connection.execute("DELETE FROM partitions WHERE end<?", (cutoff,))
            connection.execute("DELETE FROM coverage WHERE end<?", (cutoff,))
            connection.execute("DELETE FROM partition_rows WHERE end<?", (cutoff,))
            connection.execute(
                "DELETE FROM financials WHERE report_date<?", ((observed_at.date() - timedelta(days=550)).isoformat(),)
            )
            connection.execute(
                "DELETE FROM news_candidates WHERE selected_at<?", ((observed_at - timedelta(days=7)).isoformat(),)
            )
        return len(codes)


def _key(partition: DisclosurePartition) -> tuple[str, str, str]:
    return partition.market, partition.start.isoformat(), partition.end.isoformat()


def NeedsDocumentReview(title: str) -> bool:
    return any(
        marker in title
        for marker in (
            "立案",
            "调查",
            "处罚",
            "处分",
            "违法",
            "造假",
            "虚假",
            "资金占用",
            "违规担保",
            "违法担保",
            "退市",
            "终止上市",
            "减持",
        )
    )


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


def _build_snapshot(connection: sqlite3.Connection, code: str, now: datetime) -> CompanyEvidenceSnapshot:
    start, end = now.date() - timedelta(days=365), now.date()
    rows = connection.execute(
        "SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
        "WHERE c.code=? AND d.published_at>=? AND d.published_at<=? ORDER BY d.published_at,d.id",
        (code, start.isoformat(), now.isoformat()),
    ).fetchall()
    documents = tuple(
        CompanyDocument(_disclosure(row), row["body"], bool(row["body_checked"]))
        for row in rows
        if row["kind"] == "announcement"
    )
    ranges = connection.execute(
        "SELECT start,end FROM coverage WHERE market=? AND end>=? ORDER BY start",
        (EvidenceMarket(code), start.isoformat()),
    ).fetchall()
    frontier = start
    for row in ranges:
        if date.fromisoformat(row[0]) <= frontier:
            frontier = max(frontier, date.fromisoformat(row[1]) + timedelta(days=1))
    complete = frontier > end and all(
        item.checked or not NeedsDocumentReview(item.disclosure.title) for item in documents
    )
    financials = tuple(
        CompanyFinancial(row[0], date.fromisoformat(row[1]), datetime.fromisoformat(row[2]), *tuple(row)[3:])
        for row in connection.execute(
            "SELECT * FROM financials WHERE code=? AND report_date>=? AND published_at<=? ORDER BY report_date",
            (code, (end - timedelta(days=550)).isoformat(), now.isoformat()),
        )
    )
    pledge_snapshot = connection.execute("SELECT start FROM ratio_snapshots WHERE component='pledge'").fetchone()
    pledge_row = connection.execute(
        "SELECT code,day,ratio_pct FROM ratios WHERE component='pledge' AND code=? AND day<=? ORDER BY day DESC LIMIT 1",
        (code, end.isoformat()),
    ).fetchone()
    pledge = CompanyRatio(pledge_row[0], date.fromisoformat(pledge_row[1]), pledge_row[2]) if pledge_row else None
    if pledge is None and pledge_snapshot:
        pledge = CompanyRatio(code, date.fromisoformat(pledge_snapshot[0]), 0.0)
    unlocks = tuple(
        CompanyRatio(row[0], date.fromisoformat(row[1]), row[2])
        for row in connection.execute(
            "SELECT code,day,ratio_pct FROM ratios WHERE component='unlock' AND code=? AND day BETWEEN ? AND ? ORDER BY day",
            (code, end.isoformat(), (end + timedelta(days=90)).isoformat()),
        )
    )
    checks = connection.execute(
        "SELECT component,checked_at,failure FROM component_checks WHERE scope='market' OR scope=?", (code,)
    ).fetchall()
    failures = tuple(f"{row[0]}:{row[2]}" for row in checks if row[2])
    for component in ("financial", "pledge", "unlock"):
        if not any(
            row[0] == component and row[1] and datetime.fromisoformat(row[1]) >= now - timedelta(days=1) and not row[2]
            for row in checks
        ):
            failures += (f"{component}:not_current",)
    if pledge is not None and pledge.day < end - timedelta(days=10):
        failures += ("pledge:source_stale",)
    partition_errors = connection.execute(
        "SELECT DISTINCT failure FROM partitions WHERE market=? AND end>=? AND failure<>''",
        (EvidenceMarket(code), start.isoformat()),
    ).fetchall()
    failures += tuple(f"announcements:{row[0]}" for row in partition_errors)
    complete = complete and not partition_errors
    if not complete:
        failures += ("announcements:window_incomplete",)
    news = tuple(
        _disclosure(row)
        for row in rows
        if row["kind"] == "news" and datetime.fromisoformat(row["published_at"]) >= now - timedelta(days=7)
    )
    snapshot = CompanyEvidenceSnapshot(
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
    )
    version = CompanySnapshotDigest(snapshot)
    return replace(snapshot, version=version)


class SQLiteCompanyEvidenceReader:
    """Only the published, indexed projection is visible to recommendation."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self, codes: Sequence[str]) -> tuple[CompanyEvidenceSnapshot, ...]:
        snapshots: list[CompanyEvidenceSnapshot] = []
        self.visit(codes, snapshots.append)
        return tuple(snapshots)

    def visit(self, codes: Sequence[str], consume: Callable[[CompanyEvidenceSnapshot], None]) -> None:
        if not self.path.exists():
            return
        try:
            connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
            try:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                contract = connection.execute("SELECT identity FROM contract").fetchall()
                if contract != [("company_evidence",)]:
                    raise ValueError("company_snapshot_contract_mismatch")
                for code in codes:
                    row = connection.execute(
                        "SELECT version,published_at,payload FROM published WHERE code=?", (code,)
                    ).fetchone()
                    if row:
                        snapshot = DecodeCompanySnapshot(row[2])
                        if (
                            snapshot.code != code
                            or snapshot.version != row[0]
                            or snapshot.published_at.isoformat() != row[1]
                        ):
                            raise ValueError("company_snapshot_identity_mismatch")
                        consume(snapshot)
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise RuntimeError("company_snapshot_unreadable") from exc


__all__ = ["NeedsDocumentReview", "SQLiteCompanyEvidenceIndex", "SQLiteCompanyEvidenceReader"]
