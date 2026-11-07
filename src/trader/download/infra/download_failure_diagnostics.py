"""Bounded failure evidence without remote messages, paths, bodies or successful progress."""

from __future__ import annotations

from trader.infra.progress_format import CleanProgressText, ProgressDuration, ShortProgressText

import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from trader.download.domain.download_failure import (
    DownloadFailureConfiguration, DownloadFailureContext, DownloadFailureObservation,
    DownloadFailureRecord, DownloadValidationFailure,
)
from trader.download.domain.company_evidence import CompanyDocumentFailure
from trader.download.infra.history_sync_failure import HistoryFailureCauses, HistoryFailureDetail, HistoryRemoteFailureError
from trader.infra.http_contracts import HttpCancelled, HttpStatusError
from trader.infra.sqlite_connection import SqliteConnection
from trader.download.application.progress_message import ProgressField, ProgressLevel, ProgressMessage, ProgressTask

_LOGGER = logging.getLogger(__name__)
_SAFE_CODE = re.compile(r"(?:tencent|qfq|supplier|history|http|company|disclosure|cninfo)_[a-z0-9_]{1,56}")
FAILURE_DIAGNOSTICS_SQL = (
    "CREATE TABLE IF NOT EXISTS download_failure("
    "id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,occurred_at TEXT NOT NULL,stage TEXT NOT NULL,"
    "code TEXT NOT NULL,supplier TEXT NOT NULL,attempt INTEGER NOT NULL,maximum_attempts INTEGER NOT NULL,"
    "elapsed_seconds REAL,timeout_seconds REAL,reason TEXT NOT NULL,causes TEXT NOT NULL,diagnostic TEXT NOT NULL,"
    "window_start TEXT,window_end TEXT,endpoint TEXT NOT NULL)"
)


def _reason(exc: BaseException) -> str:
    if isinstance(exc, DownloadValidationFailure):
        return str(exc)
    if isinstance(exc, HttpStatusError):
        return f"http_{exc.status_code}"
    message = str(exc)
    return message if _SAFE_CODE.fullmatch(message) else type(exc).__name__[:64]


class DownloadFailureDiagnostics:
    def __init__(
        self,
        persist: Callable[[DownloadFailureRecord], None],
        clock: Callable[[], datetime],
        *,
        report: Callable[[ProgressMessage], None] | None = None,
        owner: str = "market",
        observe: Callable[[DownloadFailureObservation], None] | None = None,
    ) -> None:
        self._persist = persist
        self._clock = clock
        self._run_id = uuid4().hex
        self._report = report
        self._owner = owner
        self._observe = observe

    @property
    def run_id(self) -> str:
        return self._run_id

    def __call__(self, context: DownloadFailureContext, exc: BaseException) -> None:
        if isinstance(exc, (KeyboardInterrupt, HttpCancelled)) or str(exc) in {"cancelled", "supplier_cancelled", "tencent_qfq_cancelled"}:
            return
        reasons: list[str] = []
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen and len(reasons) < 4:
            seen.add(id(current))
            if isinstance(current, HistoryRemoteFailureError):
                reasons.extend(HistoryFailureCauses(current.evidence)[:4 - len(reasons)])
                break
            reason = _reason(current)
            if isinstance(current, OSError) and current.errno is not None:
                reason += f":errno_{current.errno}"
            reasons.append(reason)
            current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        observed = self._clock()
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("download_failure_clock_requires_timezone")
        record = DownloadFailureRecord(
            self._run_id, observed, context, _reason(exc), " > ".join(reasons),
            _diagnostic(exc, context),
        )
        if self._observe is not None:
            self._observation(DownloadFailureObservation(record))
        elif self._report is not None:
            details = (
                f"实际操作 {context.stage}", f"股票 {context.code or '不适用'}", f"来源 {context.supplier}",
                f"尝试 {context.attempt}/{context.maximum_attempts}", f"原因 {record.reason}",
                f"诊断 {HistoryFailureDetail(exc)}",
                *( (f"调用耗时 {ProgressDuration(context.elapsed_seconds)}",) if context.elapsed_seconds is not None else ()),
                *( (f"单次超时 {context.timeout_seconds}秒",) if context.timeout_seconds is not None else ()),
                *( (f"请求窗口 {context.window_start}～{context.window_end}",) if context.window_start is not None else ()),
                "后续 原始失败保留，具体续传状态见本轮汇总",
            )
            self._report(ProgressMessage(tuple(ProgressField(item, ProgressLevel.ERROR) for item in details),
                                         task=ProgressTask(self._owner, f"failure:{context.stage}", "失败诊断"),
                                         state="失败", finished=True))
        try:
            self._persist(record)
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as recording_error:
            # A full/locked/corrupt disk can also reject its own diagnostic. Keep
            # the original failure and make this evidence loss explicit.
            if self._observe is not None:
                self._observation(DownloadFailureObservation(record, "failed", HistoryFailureDetail(recording_error)))
            else:
                _LOGGER.error("download_failure_recording_failed stage=%s reason=%s", context.stage, record.reason)
        else:
            if self._observe is not None:
                self._observation(DownloadFailureObservation(record, "saved"))

    def _observation(self, observation: DownloadFailureObservation) -> None:
        if self._observe is not None:
            try:
                self._observe(observation)
            except (OSError, RuntimeError, ValueError) as exc:
                _LOGGER.error("download_failure_observation_failed reason=%s", _reason(exc))


def _diagnostic(exc: BaseException, context: DownloadFailureContext) -> str:
    parts = [HistoryFailureDetail(exc)]
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(4):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, DownloadValidationFailure):
            parts.append(f"validation field={current.field} expected={current.expected} actual={current.actual}")
        if isinstance(current, CompanyDocumentFailure) and current.diagnostic is not None:
            diagnostic = current.diagnostic
            confidence = "未采集" if diagnostic.confidence is None else f"{diagnostic.confidence:.3f}"
            parts.append(
                f"ocr_phase={diagnostic.phase} ocr_page={diagnostic.page} ocr_pages={diagnostic.pages} "
                f"ocr_characters={diagnostic.characters} ocr_confidence={confidence} "
                f"ocr_segmentation_mode={diagnostic.segmentation_mode}"
            )
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    for name, value in (
        ("operation_id", context.operation_id), ("operation_kind", context.operation_kind),
        ("component", context.component),
        ("object_id", context.object_id), ("revision", context.revision), ("market", context.market),
        ("name", context.name), ("title", context.title),
        ("page", context.page), ("page_size", context.page_size), ("queued_seconds", context.queued_seconds),
        ("raw_saved", context.raw_saved), ("text_extracted", context.text_extracted),
        ("collection_committed", context.collection_committed), ("publication_committed", context.publication_committed),
    ):
        if value is not None and value != "":
            text = str(value)
            safe = (ShortProgressText(CleanProgressText(text), 48) if name in {"name", "title"}
                    else text if re.fullmatch(r"[A-Za-z0-9_.:+-]{1,120}", text) else "not_collected")
            parts.append(f"{name}={safe}")
    if context.detail:
        parts.append(context.detail)
    return " > ".join(parts)[:2000 if len(parts) > 2 else 1000]


def WriteDownloadFailure(connection: SqliteConnection, record: DownloadFailureRecord, configuration: DownloadFailureConfiguration) -> None:
    context = record.context
    connection.run(
        "INSERT INTO download_failure(run_id,occurred_at,stage,code,supplier,attempt,maximum_attempts,"
        "elapsed_seconds,timeout_seconds,reason,causes,diagnostic,window_start,window_end,endpoint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (record.run_id, record.occurred_at.isoformat(), context.stage, context.code, context.supplier,
         context.attempt, context.maximum_attempts, context.elapsed_seconds, context.timeout_seconds,
         record.reason, record.causes, record.diagnostic,
         context.window_start.isoformat() if context.window_start else None,
         context.window_end.isoformat() if context.window_end else None, context.endpoint),
    )
    connection.run(
        "DELETE FROM download_failure WHERE id<=(SELECT id FROM download_failure ORDER BY id DESC LIMIT 1 OFFSET ?)",
        (configuration.retention_limit,),
    )


def ReadDownloadFailures(connection: SqliteConnection, configuration: DownloadFailureConfiguration) -> dict[str, object]:
    """Bounded diagnostic JSON projection; never initialize a read-only database."""
    if connection.run("SELECT 1 FROM sqlite_master WHERE name='download_failure'").fetchone() is None:
        return {"available": False, "events": [], "groups": [], "total": 0,
                "retention_limit": configuration.retention_limit, "query_limit": configuration.query_limit}
    fields = ("run_id", "occurred_at", "stage", "code", "supplier", "attempt", "maximum_attempts",
              "elapsed_seconds", "timeout_seconds", "reason", "causes", "diagnostic", "window_start", "window_end", "endpoint")
    total = connection.run("SELECT COUNT(*) FROM download_failure").require_one()[0]
    events = [dict(zip(fields, tuple(row), strict=True)) for row in connection.run(
        "SELECT " + ",".join(fields) + " FROM download_failure ORDER BY id DESC LIMIT ?", (configuration.query_limit,),
    )]
    groups = [
        {"stage": row[0], "code": row[1], "reason": row[2], "events": row[3], "first_at": row[4], "last_at": row[5]}
        for row in connection.run(
            "SELECT stage,code,reason,COUNT(*),MIN(occurred_at),MAX(occurred_at) FROM download_failure "
            "GROUP BY stage,code,reason ORDER BY COUNT(*) DESC LIMIT ?", (configuration.query_limit,),
        )
    ]
    return {"available": True, "total": total, "retention_limit": configuration.retention_limit,
            "query_limit": configuration.query_limit, "events": events, "groups": groups}
