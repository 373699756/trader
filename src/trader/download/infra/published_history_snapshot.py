"""Verified read adapter for one download-owned published history snapshot."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import groupby
from pathlib import Path

from trader.download.domain.baostock_daily import BAOSTOCK_MAX_SESSIONS
from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow
from trader.download.infra.history_control_index import HistoryControlError, HistoryControlIndex
from trader.download.infra.history_reference_index import ReferenceFileStamp
from trader.download.infra.history_snapshot_inspection import (
    HistorySnapshotInspectionError,
    LoadPublishedHistorySnapshot,
    PublishedHistorySnapshot,
)
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader, HistorySnapshotReadError


class PublishedHistoryReadError(RuntimeError):
    """The requested published snapshot is unavailable or changed identity."""


@dataclass(frozen=True)
class _CatalogSnapshot:
    history: PublishedHistorySnapshot
    reference_path: Path
    stamp: tuple[tuple[int, int, int, int, int] | None, ...]
    is_current: bool


class SQLitePublishedHistorySnapshot:
    def __init__(self, root: Path, *, sqlite_policy: SqliteIoPolicy) -> None:
        self._sqlite_policy = sqlite_policy
        self._root = root
        self._reader = HistorySnapshotReader(self._root, sqlite_policy=self._sqlite_policy)
        self._cached: _CatalogSnapshot | None = None

    def _catalog_stamp(self, reference_path: Path) -> tuple[tuple[int, int, int, int, int] | None, ...]:
        return ReferenceFileStamp(self._root / "catalog.sqlite3", reference_path)

    def _reference_path(self) -> Path:
        try:
            return HistoryControlIndex(self._root / "catalog.sqlite3", sqlite_policy=self._sqlite_policy).reference_path()
        except (HistoryControlError, OSError, ValueError) as exc:
            raise HistorySnapshotInspectionError("history_reference_unavailable") from exc

    def _load_snapshot(self, sequence: int) -> PublishedHistorySnapshot:
        cached = self._cached
        if (
            cached is not None
            and cached.stamp == self._catalog_stamp(cached.reference_path)
            and sequence == cached.history.snapshot.sequence
        ):
            return cached.history
        reference_path = self._reference_path()
        stamp = self._catalog_stamp(reference_path)
        history = LoadPublishedHistorySnapshot(self._root, sequence=sequence, sqlite_policy=self._sqlite_policy)
        if stamp == self._catalog_stamp(reference_path):
            self._cached = _CatalogSnapshot(history, reference_path, stamp, False)
        return history

    def manifest(self) -> PublishedHistoryManifest | None:
        if not (self._root / "catalog.sqlite3").is_file():
            return None
        try:
            cached = self._cached
            if cached is not None and cached.is_current and cached.stamp == self._catalog_stamp(cached.reference_path):
                history = cached.history
            else:
                # A historical pinned load cannot define the latest sequence.
                reference_path = self._reference_path()
                stamp = self._catalog_stamp(reference_path)
                history = LoadPublishedHistorySnapshot(self._root, sqlite_policy=self._sqlite_policy)
                if stamp == self._catalog_stamp(reference_path):
                    self._cached = _CatalogSnapshot(history, reference_path, stamp, True)
        except HistorySnapshotInspectionError as exc:
            if str(exc) == "history_snapshot_unavailable":
                return None
            raise PublishedHistoryReadError(str(exc)) from exc
        return PublishedHistoryManifest(
            snapshot_hash=history.snapshot.content_hash,
            sequence=history.snapshot.sequence,
            data_cutoff=history.snapshot.data_cutoff,
            calendar_dates=history.calendar.open_dates,
            universe_codes=tuple(item.code for item in history.universe.securities),
            listing_intervals=tuple(
                (item.code, item.listed_on, item.delisted_on) for item in history.universe.securities
            ),
        )

    def iter_windows(
        self,
        manifest: PublishedHistoryManifest,
        *,
        sessions: int,
        check: Callable[[], None] | None = None,
    ) -> Iterator[PublishedHistoryWindow]:
        if check is not None:
            check()
        history = self._matching_snapshot(manifest)
        dates = _session_dates(manifest, sessions)
        allowed_codes = frozenset(manifest.universe_codes)
        seen_dates: set[date] = set()
        try:
            cells = self._reader.iter_published_range_by_code(dates[0], dates[-1], history.snapshot, check=check)
            for code, group in groupby(cells, key=lambda row: row.code):
                if code in allowed_codes:
                    window = PublishedHistoryWindow(code, tuple(group))
                    seen_dates.update(cell.trade_date for cell in window.cells)
                    yield window
            if not seen_dates or any(day >= min(seen_dates) and day not in seen_dates for day in dates):
                raise PublishedHistoryReadError("history_alignment_source_day_missing")
            self._matching_snapshot(manifest)
        except (HistorySnapshotReadError, OSError, ValueError) as exc:
            raise PublishedHistoryReadError("history_snapshot_partition_invalid") from exc

    def read_windows(
        self,
        manifest: PublishedHistoryManifest,
        codes: Sequence[str],
        *,
        sessions: int,
        check: Callable[[], None] | None = None,
    ) -> tuple[PublishedHistoryWindow, ...]:
        dates = _session_dates(manifest, sessions)
        return self.read_range(manifest, codes, dates[0], dates[-1], check=check)

    def read_range(
        self,
        manifest: PublishedHistoryManifest,
        codes: Sequence[str],
        start: date,
        end: date,
        *,
        check: Callable[[], None] | None = None,
    ) -> tuple[PublishedHistoryWindow, ...]:
        if check is not None:
            check()
        if start > end or start < manifest.calendar_dates[0] or end > manifest.data_cutoff:
            raise ValueError("published history range is invalid")
        history = self._matching_snapshot(manifest)
        dates = tuple(day for day in manifest.calendar_dates if start <= day <= end)
        if not dates:
            return ()
        windows: list[PublishedHistoryWindow] = []
        allowed_codes = frozenset(manifest.universe_codes)
        try:
            for code in tuple(dict.fromkeys(codes)):
                if code not in allowed_codes:
                    continue
                rows = self._reader.read_published_code_window(code, dates, history.snapshot, check=check)
                if rows:
                    windows.append(PublishedHistoryWindow(code, rows))
            self._matching_snapshot(manifest)
        except (HistorySnapshotReadError, OSError, ValueError) as exc:
            raise PublishedHistoryReadError("history_snapshot_partition_invalid") from exc
        return tuple(windows)

    def _matching_snapshot(self, manifest: PublishedHistoryManifest) -> PublishedHistorySnapshot:
        try:
            history = self._load_snapshot(manifest.sequence)
        except HistorySnapshotInspectionError as exc:
            raise PublishedHistoryReadError(str(exc)) from exc
        if (
            history.snapshot.content_hash != manifest.snapshot_hash
            or history.calendar.open_dates != manifest.calendar_dates
            or tuple(item.code for item in history.universe.securities) != manifest.universe_codes
            or tuple((item.code, item.listed_on, item.delisted_on) for item in history.universe.securities)
            != manifest.listing_intervals
            or history.snapshot.data_cutoff != manifest.data_cutoff
        ):
            raise PublishedHistoryReadError("history_snapshot_identity_conflict")
        return history


def _session_dates(manifest: PublishedHistoryManifest, sessions: int) -> tuple[date, ...]:
    if not 1 <= sessions <= BAOSTOCK_MAX_SESSIONS:
        raise ValueError("published history session count is invalid")
    return manifest.calendar_dates[-sessions:]


__all__ = ["PublishedHistoryReadError", "SQLitePublishedHistorySnapshot"]
