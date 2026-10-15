"""Locked disk seeding and resumable online window refresh."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trader.download.application.progress_message import ProgressLevel
from trader.download.application.qfq_download_progress import QfqFailureCode, QfqMessage
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.application.update_qfq import UpdateQfqWindows
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.download.infra.qfq_progress import QfqLocalProgress
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
        started = self.updater.monotonic()
        changed: set[str] = set()
        rows = 0
        try:
            with HistoryMaintenanceLock(self.lock_path):
                recovered = (*self.v2.recover_pending_transactions(), *self.v3.recover_pending_transactions())
                if recovered:
                    self.updater.report(
                        QfqMessage(
                            self.updater.monotonic() - started,
                            "本地恢复",
                            f"已恢复事务 {len(recovered)} 个分片",
                        )
                    )
                if not seed_only:
                    return self.updater.execute(self.now())
                manifest = self.history.manifest()
                if manifest is not None:
                    for cache in (self.v2, self.v3):
                        files, deleted_rows = cache.retain_codes(frozenset(manifest.universe_codes))
                        changed.update(files)
                        rows += deleted_rows
                    needed = frozenset(manifest.universe_codes) - (self.v2.codes() & self.v3.codes())
                    if needed:
                        self.updater.report(
                            QfqMessage(
                                self.updater.monotonic() - started,
                                "历史提取",
                                f"待提取 {len(needed)} 只",
                            )
                        )
                        with QfqLocalProgress(self.updater.report):
                            seeded = self.updater.seed(
                                (
                                    window
                                    for window in self.history.iter_windows(manifest, sessions=2000)
                                    if window.code in needed
                                ),
                                f"history:{manifest.snapshot_hash}",
                            )
                        changed.update(seeded.changed_files)
                        rows += seeded.changed_rows
                return QfqUpdateResult(
                    manifest.data_cutoff if manifest is not None else None,
                    len(self.v2.codes() & self.v3.codes()),
                    changed_files=tuple(sorted(changed)),
                    changed_rows=rows,
                    failure_reason="cancelled" if self.updater.cancel_requested() else None,
                )
        except (RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
            reason = QfqFailureCode(exc)
            self.updater.report(
                QfqMessage(self.updater.monotonic() - started, "更新失败", reason, level=ProgressLevel.ERROR)
            )
            return QfqUpdateResult(None, changed_files=tuple(sorted(changed)), changed_rows=rows, failure_reason=reason)
