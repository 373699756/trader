"""Verified read adapter for one download-owned active history snapshot."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date
from itertools import groupby
from pathlib import Path

from trader.download.domain.history_revision import MAX_HISTORY_TRAINING_WINDOW_SESSIONS, HistoryRevision
from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow
from trader.download.infra.history_archive_reader import HistoryArchiveReadError, SQLiteHistoryArchiveReader
from trader.download.infra.history_archive_status import (
    ActiveHistoryArchive,
    HistoryArchiveError,
    load_active_history_archive,
)


class PublishedHistoryReadError(RuntimeError):
    """The requested published snapshot is unavailable or changed identity."""


class SQLitePublishedHistoryArchive:
    def __init__(self, root: Path) -> None:
        self._root = root

    def manifest(self) -> PublishedHistoryManifest | None:
        try:
            archive = load_active_history_archive(self._root)
        except HistoryArchiveError as exc:
            if str(exc) == "history_snapshot_unavailable":
                return None
            raise PublishedHistoryReadError(str(exc)) from exc
        return PublishedHistoryManifest(
            snapshot_hash=archive.snapshot.content_hash,
            sequence=archive.snapshot.sequence,
            data_cutoff=archive.snapshot.data_cutoff,
            calendar_dates=archive.calendar.open_dates,
            universe_codes=tuple(item.code for item in archive.universe.securities),
        )

    def iter_windows(
        self,
        manifest: PublishedHistoryManifest,
        *,
        sessions: int,
    ) -> Iterator[PublishedHistoryWindow]:
        archive = self._matching_archive(manifest)
        dates = _session_dates(manifest, sessions)
        allowed_codes = frozenset(manifest.universe_codes)
        try:
            revisions = SQLiteHistoryArchiveReader(archive.root).iter_range_by_code(
                dates[0], dates[-1], archive.snapshot
            )
            for code, group in groupby(revisions, key=lambda row: row.code):
                if code in allowed_codes:
                    yield PublishedHistoryWindow(code, tuple(group))
        except (HistoryArchiveReadError, OSError, ValueError) as exc:
            raise PublishedHistoryReadError("history_snapshot_partition_invalid") from exc

    def read_windows(
        self,
        manifest: PublishedHistoryManifest,
        codes: Sequence[str],
        *,
        sessions: int,
    ) -> tuple[PublishedHistoryWindow, ...]:
        archive = self._matching_archive(manifest)
        dates = _session_dates(manifest, sessions)
        reader = SQLiteHistoryArchiveReader(archive.root)
        windows: list[PublishedHistoryWindow] = []
        try:
            for code in tuple(dict.fromkeys(codes)):
                rows = reader.read_code_window(code, dates, archive.snapshot)
                if rows:
                    windows.append(PublishedHistoryWindow(code, rows))
        except (HistoryArchiveReadError, OSError, ValueError) as exc:
            raise PublishedHistoryReadError("history_snapshot_partition_invalid") from exc
        return tuple(windows)

    def _matching_archive(self, manifest: PublishedHistoryManifest) -> ActiveHistoryArchive:
        try:
            archive = load_active_history_archive(self._root)
        except HistoryArchiveError as exc:
            raise PublishedHistoryReadError(str(exc)) from exc
        if archive.snapshot.content_hash != manifest.snapshot_hash:
            raise PublishedHistoryReadError("history_snapshot_changed")
        return archive


def _session_dates(manifest: PublishedHistoryManifest, sessions: int) -> tuple[date, ...]:
    if not 1 <= sessions <= MAX_HISTORY_TRAINING_WINDOW_SESSIONS:
        raise ValueError("published history session count is invalid")
    return manifest.calendar_dates[-sessions:]


__all__ = ["PublishedHistoryReadError", "SQLitePublishedHistoryArchive"]
