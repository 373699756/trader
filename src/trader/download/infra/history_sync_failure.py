"""Bounded failure diagnostics without exception payloads, paths or local variables."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from trader.download.domain.history_sync import HistorySyncProgressStage
from trader.infra.workers import WorkerResourceRejectedError


class HistorySyncFailureError(RuntimeError):
    """An actual failed operation, never the location of the last progress event."""

    def __init__(self, cause: Exception, stage: HistorySyncProgressStage, item: str | None = None) -> None:
        super().__init__(history_failure_code(cause))
        self.stage = stage
        self.item = item


def history_failure_code(exc: BaseException) -> str:
    value = str(exc).strip()
    if re.fullmatch(r"[a-z0-9_]{1,64}", value):
        return value
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        # Exception chains can be cyclic; diagnostics must remain bounded.
        for _ in range(4):
            if cause.__cause__ is None or cause.__cause__ is cause:
                break
            cause = cause.__cause__
    failure = cause or exc
    if isinstance(failure, sqlite3.Error):
        return "history_sqlite_failed"
    if isinstance(failure, WorkerResourceRejectedError):
        return "history_worker_unavailable"
    if isinstance(failure, OSError):
        return "history_io_failed"
    if isinstance(failure, (TypeError, ValueError)):
        return "history_validation_failed"
    return "history_runtime_failed"


def history_failure_detail(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < 4:
        seen.add(id(current))
        name = type(current).__name__
        if isinstance(current, sqlite3.Error):
            sqlite_name = getattr(current, "sqlite_errorname", None)
            if isinstance(sqlite_name, str) and re.fullmatch(r"SQLITE_[A-Z0-9_]{1,48}", sqlite_name):
                name += f":{sqlite_name}"
            else:
                sqlite_code = getattr(current, "sqlite_errorcode", None)
                if isinstance(sqlite_code, int) and not isinstance(sqlite_code, bool) and 0 <= sqlite_code <= 65535:
                    name += f":SQLITE_CODE_{sqlite_code}"
        trace = current.__traceback__
        if trace is not None:
            while trace.tb_next is not None:
                trace = trace.tb_next
            code = trace.tb_frame.f_code
            name += f":{Path(code.co_filename).name}:{code.co_name}:{trace.tb_lineno}"
        parts.append(re.sub(r"[^0-9A-Za-z_.:-]", "_", name)[:120])
        current = current.__cause__
    return " > ".join(parts)


__all__ = ["HistorySyncFailureError", "history_failure_code", "history_failure_detail"]
