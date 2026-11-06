"""Bounded failure diagnostics without exception payloads, paths or local variables."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from trader.download.domain.history_sync import HistorySyncProgressStage
from trader.infra.workers import WorkerResourceRejectedError


class HistorySyncFailureError(RuntimeError):
    """An actual failed operation, never the location of the last progress event."""

    def __init__(self, cause: Exception, stage: HistorySyncProgressStage, item: str | None = None) -> None:
        super().__init__(HistoryFailureCode(cause))
        self.stage = stage
        self.item = item


@dataclass(frozen=True)
class HistoryFailureCause:
    exception_type: str
    error_number: int | None = None
    sqlite_code: str | None = None
    location: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", self.exception_type):
            raise ValueError("history_failure_type_invalid")
        if self.error_number is not None and (type(self.error_number) is not int or abs(self.error_number) > 65535):
            raise ValueError("history_failure_errno_invalid")
        if self.sqlite_code is not None and not re.fullmatch(r"SQLITE_[A-Z0-9_]{1,48}", self.sqlite_code):
            raise ValueError("history_failure_sqlite_code_invalid")
        if self.location is not None and not re.fullmatch(r"[0-9A-Za-z_.:-]{1,120}", self.location):
            raise ValueError("history_failure_location_invalid")


@dataclass(frozen=True)
class HistoryFailureEvidence:
    """IPC-safe cause metadata, without exception messages, arguments or locals."""

    causes: tuple[HistoryFailureCause, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.causes, tuple) or not 1 <= len(self.causes) <= 4:
            raise ValueError("history_failure_causes_invalid")
        if not all(isinstance(cause, HistoryFailureCause) for cause in self.causes):
            raise ValueError("history_failure_cause_invalid")


class HistoryRemoteFailureError(RuntimeError):
    """Preserve the supplier's business code and its safe remote cause evidence."""

    def __init__(self, reason: str, evidence: HistoryFailureEvidence) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", reason):
            raise ValueError("history_remote_failure_reason_invalid")
        super().__init__(reason)
        self.evidence = evidence


def HistoryFailureCode(exc: BaseException) -> str:
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


def HistoryFailureDetail(exc: BaseException) -> str:
    return " > ".join(_project_cause(cause, include_location=True) for cause in BuildHistoryFailureEvidence(exc).causes)


def HistoryFailureCauses(evidence: HistoryFailureEvidence) -> tuple[str, ...]:
    return tuple(_project_cause(cause, include_location=False) for cause in evidence.causes)


def BuildHistoryFailureEvidence(exc: BaseException) -> HistoryFailureEvidence:
    causes: list[HistoryFailureCause] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(causes) < 4:
        seen.add(id(current))
        if isinstance(current, HistoryRemoteFailureError):
            causes.extend(current.evidence.causes[:4 - len(causes)])
            break
        name = re.sub(r"[^0-9A-Za-z_]", "_", type(current).__name__)[:64]
        if not name or name[0].isdigit():
            name = "_" + name[:63]
        error_number = None
        if isinstance(current, OSError) and type(current.errno) is int and abs(current.errno) <= 65535:
            error_number = current.errno
        sqlite_code = None
        if isinstance(current, sqlite3.Error):
            sqlite_name = getattr(current, "sqlite_errorname", None)
            if isinstance(sqlite_name, str) and re.fullmatch(r"SQLITE_[A-Z0-9_]{1,48}", sqlite_name):
                sqlite_code = sqlite_name
            else:
                numeric_code = getattr(current, "sqlite_errorcode", None)
                if type(numeric_code) is int and 0 <= numeric_code <= 65535:
                    sqlite_code = f"SQLITE_CODE_{numeric_code}"
        trace = current.__traceback__
        location = None
        if trace is not None:
            while trace.tb_next is not None:
                trace = trace.tb_next
            code = trace.tb_frame.f_code
            location = re.sub(r"[^0-9A-Za-z_.:-]", "_", f"{Path(code.co_filename).name}:{code.co_name}:{trace.tb_lineno}")[:120]
        causes.append(HistoryFailureCause(name, error_number, sqlite_code, location))
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return HistoryFailureEvidence(tuple(causes))


def _project_cause(cause: HistoryFailureCause, *, include_location: bool) -> str:
    value = cause.exception_type
    if cause.sqlite_code is not None:
        value += ":" + cause.sqlite_code
    if cause.error_number is not None:
        value += f":errno_{cause.error_number}"
    if include_location and cause.location is not None:
        value += ":" + cause.location
    return value


__all__ = [
    "BuildHistoryFailureEvidence",
    "HistoryFailureCause",
    "HistoryFailureCauses",
    "HistoryFailureEvidence",
    "HistoryRemoteFailureError",
    "HistorySyncFailureError",
    "HistoryFailureCode",
    "HistoryFailureDetail",
]
