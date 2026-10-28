"""Snapshot-bound reference links and historical industry projection."""

from __future__ import annotations

import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Collection
from dataclasses import replace
from datetime import date
from pathlib import Path

from trader.download.domain.baostock_daily import BaoStockIndustryInterval
from trader.download.domain.history_reference import HistoryReferenceSnapshot
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.infra.sqlite_codec import DecodeDate, EncodeDate, EncodeHash
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction


class HistoryReferenceIndex:
    def __init__(self, reference: HistoryReferenceSnapshot) -> None:
        self.reference = reference
        self._eligible = frozenset(reference.eligible_codes)
        grouped: dict[str, list[BaoStockIndustryInterval]] = defaultdict(list)
        for item in reference.industry_intervals:
            grouped[item.code].append(item)
        self._intervals = {code: tuple(values) for code, values in grouped.items()}
        self._starts = {code: tuple(item.effective_from for item in values) for code, values in self._intervals.items()}

    def apply(self, revision: HistoryRevision) -> HistoryRevision:
        industry = self.industry_on(revision.code, revision.trade_date)
        return replace(
            revision,
            is_st=False if revision.code in self._eligible else None,
            industry=industry.industry if industry is not None else None,
            industry_classification=industry.classification if industry is not None else None,
        )

    def eligible(self, code: str) -> bool:
        return code in self._eligible

    def industry_on(self, code: str, day: date) -> BaoStockIndustryInterval | None:
        values = self._intervals.get(code, ())
        position = bisect_right(self._starts.get(code, ()), day) - 1
        industry = values[position] if position >= 0 else None
        if industry is not None and industry.effective_to is not None and day >= industry.effective_to:
            industry = None
        return industry

    def industry_sample_upper_bounds(
        self, codes: Collection[str], dates: tuple[date, ...], required_sessions: int, anchors: frozenset[date]
    ) -> tuple[tuple[str, int], ...]:
        """Conservative upper bounds: price, listing and interior gaps can only reduce them."""
        if len(dates) < required_sessions:
            return ()
        allowed = tuple(day for day in dates[required_sessions - 1 :] if day in anchors)
        counts: dict[str, int] = defaultdict(int)
        for code in codes:
            if code not in self._eligible:
                continue
            for interval in self._intervals.get(code, ()):
                start = bisect_left(allowed, interval.effective_from)
                end = bisect_left(allowed, interval.effective_to) if interval.effective_to else len(allowed)
                counts[interval.industry] += max(0, end - start)
        return tuple(sorted(counts.items()))

    def has_industry_window(
        self,
        codes: Collection[str],
        calendar_dates: tuple[date, ...],
        required_sessions: int,
        *,
        anchor_dates: frozenset[date] | None = None,
    ) -> bool:
        """Return whether any eligible code has one continuous dated-industry window."""

        dates = tuple(calendar_dates)
        if not dates or dates != tuple(sorted(set(dates))) or required_sessions < 1 or required_sessions > len(dates):
            return False
        anchors = tuple(index for index, day in enumerate(dates) if anchor_dates is None or day in anchor_dates)
        for code in codes:
            if code not in self._eligible:
                continue
            covered_start: int | None = None
            covered_end = 0
            for interval in self._intervals.get(code, ()):
                start = bisect_left(dates, interval.effective_from)
                end = bisect_left(dates, interval.effective_to) if interval.effective_to is not None else len(dates)
                if end <= start:
                    continue
                if covered_start is None or start > covered_end:
                    covered_start = start
                    covered_end = end
                else:
                    covered_end = max(covered_end, end)
                position = bisect_left(anchors, covered_start + required_sessions - 1)
                if position < len(anchors) and anchors[position] < covered_end:
                    return True
        return False


def WriteHistoryReference(
    connection: SqliteConnection,
    reference: HistoryReferenceSnapshot,
    index: SQLiteReferenceIndex,
    publication_id: str,
    *,
    references: dict[str, PreparedReference] | None = None,
) -> None:
    published = index.read(publication_id)
    if (
        published is None
        or published.st_evidence != reference.st_evidence
        or tuple(item.code for item in published.eligible_universe) != reference.eligible_codes
    ):
        raise ValueError("history_reference_publication_conflict")
    if references is None:
        references = {}
    references[publication_id] = published
    digest = EncodeHash(reference.content_hash)
    existing = connection.run(
        "SELECT publication_id FROM reference_binding WHERE reference_hash=?", (digest,)
    ).fetchone()
    if existing is not None:
        try:
            previous = _read(connection, reference.content_hash, index, references)
        except ValueError as exc:
            raise ValueError("history_reference_content_conflict") from exc
        if previous != reference:
            raise ValueError("history_reference_content_conflict")
        return
    connection.run("INSERT INTO reference_binding VALUES(?,?)", (digest, publication_id))
    connection.run_many(
        "INSERT OR IGNORE INTO history_industry VALUES(?,?,?,?,?,?)",
        (
            (
                digest,
                item.code,
                EncodeDate(item.effective_from),
                EncodeDate(item.effective_to) if item.effective_to else None,
                item.industry,
                item.classification,
            )
            for item in reference.industry_intervals
        ),
    )
    if _read(connection, reference.content_hash, index, references) != reference:
        raise ValueError("history_reference_content_conflict")


def ReadHistoryReference(
    path: Path, digest: str, *, references: dict[str, PreparedReference] | None = None
) -> HistoryReferenceSnapshot:
    try:
        with (
            SqliteSession(path, SqliteConnectionOptions(read_only=True)) as connection,
            SqliteTransaction(connection),
        ):
            location = connection.run("SELECT reference_file FROM schema_contract WHERE singleton=1").fetchone()
            if location is None or location[0] is None:
                raise ValueError("history_reference_location_missing")
            index = SQLiteReferenceIndex((path.parent / location[0]).resolve())
            return _read(connection, digest, index, references)
    except (TypeError, ValueError, sqlite3.Error) as exc:
        raise ValueError("history_reference_unavailable") from exc


def _read(
    connection: SqliteConnection,
    digest: str,
    index: SQLiteReferenceIndex,
    references: dict[str, PreparedReference] | None = None,
) -> HistoryReferenceSnapshot:
    row = connection.run(
        "SELECT publication_id FROM reference_binding WHERE reference_hash=?", (EncodeHash(digest),)
    ).fetchone()
    if row is None:
        raise ValueError("history_reference_missing")
    published = references.get(row[0]) if references is not None else None
    if published is None:
        published = index.read(row[0])
    if published is None:
        raise ValueError("history_reference_missing")
    if references is not None:
        references[row[0]] = published
    intervals = tuple(
        BaoStockIndustryInterval(
            item[0],
            DecodeDate(item[1]),
            DecodeDate(item[2]) if item[2] is not None else None,
            item[3],
            item[4],
        )
        for item in connection.run(
            "SELECT code,start_date,end_date,industry,classification FROM history_industry "
            "WHERE reference_hash=? ORDER BY code,start_date",
            (EncodeHash(digest),),
        )
    )
    reference = HistoryReferenceSnapshot(
        tuple(item.code for item in published.eligible_universe),
        intervals,
        published.st_evidence,
    )
    if reference.content_hash != digest:
        raise ValueError("history_reference_hash_conflict")
    return reference


def ReadBoundHistoryReference(
    root: Path, supplier_contract: str, *, references: dict[str, PreparedReference] | None = None
) -> HistoryReferenceSnapshot:
    if not supplier_contract.startswith("history_ref_"):
        raise ValueError("history_reference_contract_invalid")
    digest = supplier_contract[12:76]
    EncodeHash(digest)
    return ReadHistoryReference(root / "catalog.sqlite3", digest, references=references)


def ReferenceFileStamp(catalog: Path, reference: Path) -> tuple[tuple[int, int, int, int, int] | None, ...]:
    """Include both databases and committed WAL pages when caching a verified binding."""
    stamps: list[tuple[int, int, int, int, int] | None] = []
    for path in (catalog, Path(str(catalog) + "-wal"), reference, Path(str(reference) + "-wal")):
        try:
            stat = path.stat()
        except FileNotFoundError:
            stamps.append(None)
        else:
            stamps.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(stamps)


__all__ = [
    "HistoryReferenceIndex",
    "ReadBoundHistoryReference",
    "ReadHistoryReference",
    "WriteHistoryReference",
    "ReferenceFileStamp",
]
