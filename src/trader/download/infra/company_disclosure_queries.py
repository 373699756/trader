"""SQL operations for child queries, executed only by the company index writer."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

from trader.download.domain.company_evidence import (
    DisclosureCoverageMismatch,
    DisclosurePage,
    DisclosurePartition,
    DisclosureQuery,
    DisclosureSecurity,
)
from trader.infra.sqlite_connection import SqliteConnection


def DisclosurePartitionKey(partition: DisclosurePartition) -> tuple[str, str, str]:
    return partition.market, partition.start.isoformat(), partition.end.isoformat()


def DisclosureQueryKey(partition: DisclosurePartition) -> tuple[str, str, str, str]:
    return (*DisclosurePartitionKey(partition), partition.query.key if partition.query is not None else "")


def ActivateDisclosureQueryPolicy(connection: SqliteConnection) -> None:
    identity = "issuer_union_board"
    row = connection.run("SELECT query_policy_id FROM disclosure_query_policy WHERE singleton=1").fetchone()
    if row is not None and row[0] == identity:
        return
    # Retry only failures affected by this parser/union correction, once. Keep
    # network cooldowns, completed pages, facts and published snapshots intact.
    connection.run(
        "DELETE FROM partition_retries WHERE (scope='' AND EXISTS(SELECT 1 FROM partitions p "
        "WHERE p.market=partition_retries.market AND p.start_date=partition_retries.start_date AND p.end_date=partition_retries.end_date "
        "AND p.failure IN ('cninfo_query_coverage_mismatch','cninfo_query_filter_ignored'))) OR EXISTS(SELECT 1 FROM disclosure_queries q "
        "WHERE q.market=partition_retries.market AND q.start_date=partition_retries.start_date AND q.end_date=partition_retries.end_date "
        "AND q.scope=partition_retries.scope AND q.failure='cninfo_query_filter_ignored')"
    )
    connection.run("INSERT OR REPLACE INTO disclosure_query_policy VALUES(1,?)", (identity,))


def ReadDisclosureQueries(
    connection: SqliteConnection, partition: DisclosurePartition, *, include_completed: bool = False
) -> tuple[DisclosurePartition, ...] | None:
    rows = connection.run(
        "SELECT * FROM disclosure_queries WHERE market=? AND start_date=? AND end_date=? "
        "ORDER BY CASE WHEN next_page>1 THEN 0 ELSE 1 END,scope",
        DisclosurePartitionKey(partition),
    ).fetchall()
    if not rows:
        return None
    return tuple(
        replace(
            partition,
            page=row["next_page"],
            expected_rows=row["expected_rows"],
            received_rows=row["received_rows"],
            query=DisclosureQuery(row["plate"], _decode_securities(row["securities"])),
        )
        for row in rows
        if include_completed or not row["is_complete"]
    )


def SeedDisclosureQueries(
    connection: SqliteConnection,
    partition: DisclosurePartition,
    queries: tuple[DisclosureQuery, ...],
) -> None:
    key = DisclosurePartitionKey(partition)
    # A split owns no coverage until every replacement query is complete and reconciled.
    connection.run("UPDATE partitions SET is_complete=0 WHERE market=? AND start_date=? AND end_date=?", key)
    connection.run("DELETE FROM coverage WHERE market=? AND start_date=? AND end_date=?", key)
    if partition.query is not None:
        connection.run(
            "DELETE FROM disclosure_queries WHERE market=? AND start_date=? AND end_date=? AND scope=?",
            DisclosureQueryKey(partition),
        )
    for query in queries:
        connection.run(
            "INSERT OR IGNORE INTO disclosure_queries(market,start_date,end_date,scope,plate,securities) VALUES(?,?,?,?,?,?)",
            (*key, query.key, query.plate, json.dumps([[s.code, s.org_id] for s in query.securities])),
        )


def ValidateDisclosureQueryPage(
    connection: SqliteConnection, partition: DisclosurePartition, page: DisclosurePage
) -> None:
    if partition.expected_rows is not None and partition.expected_rows != page.total:
        raise ValueError("cninfo_partition_changed")
    received = partition.received_rows + len(page.rows)
    if not page.has_more and received != page.total:
        raise ValueError("cninfo_page_truncated")
    if page.has_more and received >= page.total:
        raise ValueError("cninfo_page_count_inconsistent")
    links = tuple((item.identity, code) for item in page.rows for code in item.codes)
    if len(links) != len(set(links)) or (
        partition.page > 1
        and any(
            connection.run(
                "SELECT 1 FROM disclosure_query_rows WHERE market=? AND start_date=? AND end_date=? AND scope=? AND source_key=? AND code=?",
                (*DisclosureQueryKey(partition), identity, code),
            ).fetchone()
            is not None
            for identity, code in links
        )
    ):
        raise ValueError("cninfo_duplicate_page_links")


def CommitDisclosureQueryPage(
    connection: SqliteConnection, partition: DisclosurePartition, page: DisclosurePage
) -> None:
    key = DisclosureQueryKey(partition)
    if partition.page == 1:
        connection.run("DELETE FROM disclosure_query_rows WHERE market=? AND start_date=? AND end_date=? AND scope=?", key)
    connection.run_many(
        "INSERT INTO disclosure_query_rows VALUES(?,?,?,?,?,?,?)",
        (
            (*key, item.identity, code, json.dumps([item.identity, item.codes], separators=(",", ":")))
            for item in page.rows
            for code in item.codes
        ),
    )
    connection.run(
        "UPDATE disclosure_queries SET next_page=?,expected_rows=?,received_rows=?,is_complete=?,failure='' "
        "WHERE market=? AND start_date=? AND end_date=? AND scope=?",
        (partition.page + 1, page.total, partition.received_rows + len(page.rows), int(not page.has_more), *key),
    )
    connection.run("DELETE FROM partition_retries WHERE market=? AND start_date=? AND end_date=? AND scope=?", key)


def ReconcileDisclosureQueries(
    connection: SqliteConnection, partition: DisclosurePartition, page: DisclosurePage
) -> None:
    key = DisclosurePartitionKey(partition)
    counts = connection.run(
        "SELECT count(*),sum(is_complete),sum(expected_rows) FROM disclosure_queries WHERE market=? AND start_date=? AND end_date=?",
        key,
    ).require_one()
    unique = connection.run(
        "SELECT count(DISTINCT record_key) FROM disclosure_query_rows WHERE market=? AND start_date=? AND end_date=?", key
    ).require_one()[0]
    # Issuer aliases can make separate stock filters return the same records.
    # Only the exact union, not the sum of overlapping query counts, proves coverage.
    if not counts[0] or counts[0] != counts[1] or counts[2] < page.total or unique != page.total:
        raise DisclosureCoverageMismatch(max(0, page.total - unique), refresh_required=unique > page.total)
    # Verify that the fresh parent sample is represented, not just that counts happen to agree.
    for item in page.rows:
        if (
            connection.run(
                "SELECT 1 FROM disclosure_query_rows WHERE market=? AND start_date=? AND end_date=? AND record_key=? LIMIT 1",
                (*key, json.dumps([item.identity, item.codes], separators=(",", ":"))),
            ).fetchone()
            is None
        ):
            raise DisclosureCoverageMismatch(0, refresh_required=True)
    connection.run("DELETE FROM partition_rows WHERE market=? AND start_date=? AND end_date=?", key)
    connection.run(
        "INSERT INTO partition_rows SELECT DISTINCT market,start_date,end_date,source_key,code FROM disclosure_query_rows "
        "WHERE market=? AND start_date=? AND end_date=?",
        key,
    )


def RestartDisclosureQuery(connection: SqliteConnection, partition: DisclosurePartition) -> None:
    connection.run(
        "UPDATE disclosure_queries SET next_page=1,expected_rows=NULL,received_rows=0,is_complete=0 "
        "WHERE market=? AND start_date=? AND end_date=? AND scope=?",
        DisclosureQueryKey(partition),
    )
    connection.run(
        "DELETE FROM disclosure_query_rows WHERE market=? AND start_date=? AND end_date=? AND scope=?",
        DisclosureQueryKey(partition),
    )


def DeferDisclosurePartition(
    connection: SqliteConnection, partition: DisclosurePartition, retry_after: datetime
) -> None:
    connection.run(
        "INSERT OR REPLACE INTO partition_retries VALUES(?,?,?,?,?)",
        (*DisclosureQueryKey(partition), retry_after.isoformat()),
    )


def _decode_securities(payload: str) -> tuple[DisclosureSecurity, ...]:
    values = json.loads(payload)
    if not isinstance(values, list) or any(
        not isinstance(v, list) or len(v) != 2 or not all(isinstance(s, str) for s in v) for v in values
    ):
        raise ValueError("cninfo_query_invalid")
    return tuple(DisclosureSecurity(code, org) for code, org in values)


__all__ = [
    "ActivateDisclosureQueryPolicy",
    "DisclosurePartitionKey",
    "DisclosureQueryKey",
    "ReadDisclosureQueries",
    "SeedDisclosureQueries",
    "ValidateDisclosureQueryPage",
    "CommitDisclosureQueryPage",
    "ReconcileDisclosureQueries",
    "RestartDisclosureQuery",
    "DeferDisclosurePartition",
]
