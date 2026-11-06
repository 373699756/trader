"""Immutable history download plans and publication values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from trader.download.domain.history_control import (
    HistoryCalendarIdentity,
    HistorySecurityIdentity,
    HistorySnapshot,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistoryUniverseIdentity,
)
from trader.download.domain.history_reference import HistoryReferenceSnapshot
from trader.download.domain.history_sync import HistorySupplierContext
from trader.infra.serialization.canonical_codec import CanonicalDigest

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_BAOSTOCK_DAILY_READY = time(15, 10)


@dataclass(frozen=True)
class HistoryDownloadTask:
    code: str
    dates: tuple[date, ...]
    tencent_done: bool = False
    attempts: int = 0
    failure: str | None = None

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[0-9]{6}", self.code) is None
            or self.dates != tuple(sorted(set(self.dates)))
            or any(type(day) is not date for day in self.dates)
            or type(self.tencent_done) is not bool
            or type(self.attempts) is not int
            or not 0 <= self.attempts <= 3
            or (self.failure is not None and re.fullmatch(r"[a-z0-9_]{1,64}", self.failure) is None)
        ):
            raise ValueError("history_download_task_invalid")


@dataclass(frozen=True)
class HistoryDownloadCompletion:
    task: HistoryDownloadTask
    missing: tuple[date, ...]
    failure: str | None
    unusable_qfq_dates: tuple[date, ...] = ()
    inferred_suspended_dates: tuple[date, ...] = ()
    reviewed_dates: tuple[date, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.missing != tuple(sorted(set(self.missing)))
            or not set(self.missing).issubset(self.task.dates)
            or (self.failure is not None and re.fullmatch(r"[a-z0-9_]{1,64}", self.failure) is None)
            or self.unusable_qfq_dates != tuple(sorted(set(self.unusable_qfq_dates)))
            or not set(self.unusable_qfq_dates).issubset(self.missing)
            or self.inferred_suspended_dates != tuple(sorted(set(self.inferred_suspended_dates)))
            or not set(self.inferred_suspended_dates).issubset(self.task.dates)
            or self.reviewed_dates != tuple(sorted(set(self.reviewed_dates)))
            or not set(self.reviewed_dates).issubset(self.task.dates)
            or (self.failure is not None and bool(self.reviewed_dates))
        ):
            raise ValueError("history_download_completion_invalid")


@dataclass(frozen=True)
class HistoryDownloadBatch:
    sequence: int
    source_hash: str
    calendar_hash: str
    universe_hash: str
    cutoff: date

    def __post_init__(self) -> None:
        if (
            type(self.sequence) is not int
            or self.sequence < 1
            or type(self.cutoff) is not date
            or any(
                re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in (self.source_hash, self.calendar_hash, self.universe_hash)
            )
        ):
            raise ValueError("history_download_batch_invalid")


@dataclass(frozen=True)
class HistoryBatchInput:
    context: HistorySupplierContext
    source: HistorySourceIdentity
    calendar: HistoryCalendarIdentity
    universe: HistoryUniverseIdentity
    reference: HistoryReferenceSnapshot
    batch: HistoryDownloadBatch
    fresh: bool
    reviewed: bool = False


@dataclass(frozen=True)
class HistoryPublication:
    snapshot: HistorySnapshot
    checkpoint: HistorySyncCheckpoint
    reference: HistoryReferenceSnapshot


def BuildHistoryControlIdentities(
    context: HistorySupplierContext,
) -> tuple[HistorySourceIdentity, HistoryCalendarIdentity, HistoryUniverseIdentity]:
    cutoff = context.calendar.open_dates[-1]
    observed = datetime.combine(cutoff, _BAOSTOCK_DAILY_READY, _SHANGHAI)
    reference = HistoryReferenceSnapshot(
        tuple(item.code for item in context.universe), context.industry_intervals, context.st_evidence
    )
    supplier_contract = f"history_ref_{reference.content_hash}_{CanonicalDigest(context.source_versions)[:16]}"
    source = HistorySourceIdentity("tencent_baostock", "history_daily", supplier_contract[:128], observed)
    calendar = HistoryCalendarIdentity(context.calendar.open_dates, source.content_hash)
    universe = HistoryUniverseIdentity(
        tuple(
            HistorySecurityIdentity(item.code, item.name, item.board, item.listed_on, item.delisted_on)
            for item in context.universe
        ),
        source.content_hash,
    )
    return source, calendar, universe



def PlanHistoryDownloadTasks(
    context: HistorySupplierContext,
    current_snapshot: HistorySnapshot | None,
    previous: frozenset[str],
    retryable: Mapping[str, tuple[date, ...]],
    reread: int,
) -> tuple[HistoryDownloadTask, ...]:
    tasks = []
    for security in context.universe:
        expected = context.calendar.expected_dates(security)
        if current_snapshot is None or security.code not in previous:
            dates = expected
        else:
            missing = {day for day in retryable.get(security.code, ()) if day <= context.calendar.open_dates[-1]}
            new = {day for day in expected if day > current_snapshot.data_cutoff}
            anchors = (
                tuple(day for day in expected if day <= current_snapshot.data_cutoff)[-reread:]
                if new or missing
                else ()
            )
            dates = tuple(sorted(missing | new | set(anchors)))
        tasks.append(HistoryDownloadTask(security.code, dates))
    return tuple(tasks)


__all__ = [
    "PlanHistoryDownloadTasks",
    "BuildHistoryControlIdentities",
    "HistoryBatchInput",
    "HistoryDownloadBatch",
    "HistoryDownloadCompletion",
    "HistoryDownloadTask",
    "HistoryPublication",
]
