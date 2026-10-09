"""Locked disk seeding and resumable online window refresh."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.application.update_qfq import UpdateQfqWindows
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.infra.history_control_repository import HistoryMaintenanceLock
from trader.download.infra.qfq_progress import qfq_local_progress
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache


@dataclass(frozen=True)
class QfqUpdateRunner:
    history: ReadPublishedHistoryUseCase
    updater: UpdateQfqWindows
    v2: SQLiteQfqWindowCache
    v3: SQLiteQfqWindowCache
    lock_path: Path
    now: Callable[[], datetime]

    def execute(self, *, seed_only: bool = False) -> QfqUpdateResult:
        changed: set[str] = set()
        rows = 0
        try:
            with HistoryMaintenanceLock(self.lock_path):
                try:
                    manifest = self.history.manifest()
                except (RuntimeError, OSError, ValueError, sqlite3.Error):
                    if seed_only:
                        raise
                    self.updater.report("qfq history unavailable: using BaoStock bounded windows")
                    manifest = None
                if manifest is not None:
                    needed = frozenset(manifest.universe_codes) - (self.v2.codes() & self.v3.codes())
                    if needed:
                        self.updater.report(f"qfq history extraction: missing={len(needed)}")
                        with qfq_local_progress(self.updater.report):
                            seeded = self.updater.seed(
                                (
                                    window
                                    for window in self.history.iter_windows(manifest, sessions=251)
                                    if window.code in needed
                                ),
                                f"history:{manifest.snapshot_hash}",
                            )
                        changed.update(seeded.changed_files)
                        rows += seeded.changed_rows
                if seed_only:
                    return QfqUpdateResult(
                        manifest.data_cutoff if manifest is not None else None,
                        len(self.v2.codes() & self.v3.codes()),
                        changed_files=tuple(sorted(changed)),
                        changed_rows=rows,
                        failure_reason="cancelled" if self.updater.cancel_requested() else None,
                    )
                result = self.updater.execute(self.now())
                return replace(
                    result,
                    changed_files=tuple(sorted(changed | set(result.changed_files))),
                    changed_rows=rows + result.changed_rows,
                )
        except (RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
            self.updater.report(f"qfq update failed: {type(exc).__name__}")
            return QfqUpdateResult(
                None, changed_files=tuple(sorted(changed)), changed_rows=rows, failure_reason=type(exc).__name__
            )
