"""Verified read adapter for one download-owned active history snapshot."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date
from itertools import groupby
from pathlib import Path

from trader.download.domain.baostock_daily import BAOSTOCK_MAX_SESSIONS
from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow
from trader.download.infra.history_snapshot_inspection import (
    ActiveHistorySnapshot,
    HistorySnapshotInspectionError,
    LoadPublishedHistorySnapshot,
)
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader, HistorySnapshotReadError


class PublishedHistoryReadError(RuntimeError):
    """The requested published snapshot is unavailable or changed identity."""


class SQLitePublishedHistorySnapshot:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._reader = HistorySnapshotReader(self._root)

    def manifest(self) -> PublishedHistoryManifest | None:
        try:
            archive = LoadPublishedHistorySnapshot(self._root)
        except HistorySnapshotInspectionError as exc:
            if str(exc) == "history_snapshot_unavailable":
                return None
            raise PublishedHistoryReadError(str(exc)) from exc
        return PublishedHistoryManifest(
            snapshot_hash=archive.snapshot.content_hash,
            sequence=archive.snapshot.sequence,
            data_cutoff=archive.snapshot.data_cutoff,
            calendar_dates=archive.calendar.open_dates,
            universe_codes=tuple(item.code for item in archive.universe.securities),
            listing_intervals=tuple(
                (item.code, item.listed_on, item.delisted_on) for item in archive.universe.securities
            ),
        )

    def iter_windows(
        self,
        manifest: PublishedHistoryManifest,
        *,
        sessions: int,
    ) -> Iterator[PublishedHistoryWindow]:
        archive = self._matching_snapshot(manifest)
        dates = _session_dates(manifest, sessions)
        allowed_codes = frozenset(manifest.universe_codes)
        seen_dates: set[date] = set()
        try:
            cells = self._reader.iter_published_range_by_code(dates[0], dates[-1], archive.snapshot)
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
    ) -> tuple[PublishedHistoryWindow, ...]:
        archive = self._matching_snapshot(manifest)
        dates = _session_dates(manifest, sessions)
        windows: list[PublishedHistoryWindow] = []
        allowed_codes = frozenset(manifest.universe_codes)
        try:
            for code in tuple(dict.fromkeys(codes)):
                if code not in allowed_codes:
                    continue
                rows = self._reader.read_published_code_window(code, dates, archive.snapshot)
                if rows:
                    windows.append(PublishedHistoryWindow(code, rows))
            self._matching_snapshot(manifest)
        except (HistorySnapshotReadError, OSError, ValueError) as exc:
            raise PublishedHistoryReadError("history_snapshot_partition_invalid") from exc
        return tuple(windows)

    def _matching_snapshot(self, manifest: PublishedHistoryManifest) -> ActiveHistorySnapshot:
        try:
            archive = LoadPublishedHistorySnapshot(self._root, sequence=manifest.sequence)
        except HistorySnapshotInspectionError as exc:
            raise PublishedHistoryReadError(str(exc)) from exc
        if (
            archive.snapshot.content_hash != manifest.snapshot_hash
            or archive.calendar.open_dates != manifest.calendar_dates
            or tuple(item.code for item in archive.universe.securities) != manifest.universe_codes
            or tuple((item.code, item.listed_on, item.delisted_on) for item in archive.universe.securities)
            != manifest.listing_intervals
            or archive.snapshot.data_cutoff != manifest.data_cutoff
        ):
            raise PublishedHistoryReadError("history_snapshot_identity_conflict")
        return archive


def _session_dates(manifest: PublishedHistoryManifest, sessions: int) -> tuple[date, ...]:
    if not 1 <= sessions <= BAOSTOCK_MAX_SESSIONS:
        raise ValueError("published history session count is invalid")
    return manifest.calendar_dates[-sessions:]


__all__ = ["PublishedHistoryReadError", "SQLitePublishedHistorySnapshot"]
