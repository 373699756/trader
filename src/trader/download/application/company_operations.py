"""Typed operation boundaries without owning threads, I/O, or business counters."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import replace
from threading import Lock
from typing import Literal, TypeVar

from trader.download.domain.company_evidence import (
    CompanyOperationKind,
    CompanyOperationProgress,
    Disclosure,
    EvidenceSyncProgress,
)
from trader.infra.workers import SubmitOrReject, WorkerExecutor

_Result = TypeVar("_Result")


class CompanyOperations:
    """Workers only send bounded observations; the command heartbeat owns output."""

    def __init__(
        self,
        report: Callable[[EvidenceSyncProgress], None] | None,
        monotonic: Callable[[], float],
        names: tuple[tuple[str, str], ...],
        *,
        population: tuple[str, ...] = (),
    ) -> None:
        self._report, self._monotonic, self._names = report, monotonic, names
        self._codes = frozenset(population)
        self._lock = Lock()
        self._next = 0

    def queued(
        self,
        kind: CompanyOperationKind,
        *,
        document: Disclosure | None = None,
        code: str = "",
        source: str = "local",
    ) -> CompanyOperationProgress:
        with self._lock:
            self._next += 1
            token = self._next
        if document is not None:
            code = document.codes[0]
            source = document.source
        event = CompanyOperationProgress(
            token, kind, "queued", self._monotonic(), code,
            next((name for candidate, name in self._names if candidate == code), ""),
            document.title if document is not None else "",
            source,
            related_codes=tuple(sorted(set(document.codes).intersection(self._codes))) if document is not None else (),
        )
        self._emit(event)
        return event

    @contextmanager
    def running(self, event: CompanyOperationProgress) -> Iterator[Callable[[int, int], None]]:
        current = replace(event, state="running", at=self._monotonic())
        self._emit(current)

        def page_progress(completed: int, total: int) -> None:
            nonlocal current
            current = replace(current, completed_units=completed, total_units=total, unit="pages")
            self._emit(current)

        state: Literal["completed", "failed", "cancelled"] = "failed"
        try:
            yield page_progress
            state = "completed"
        except InterruptedError:
            state = "cancelled"
            raise
        finally:
            self._emit(replace(current, state=state, at=self._monotonic()))

    def call(
        self,
        kind: CompanyOperationKind,
        operation: Callable[[], _Result],
        *,
        document: Disclosure | None = None,
        code: str = "",
        source: str = "local",
    ) -> _Result:
        event = self.queued(kind, document=document, code=code, source=source)
        with self.running(event):
            return operation()

    def cancelled(self, event: CompanyOperationProgress, future: Future[_Result]) -> None:
        if future.cancelled():
            self._emit(replace(event, state="cancelled", at=self._monotonic()))

    def submit(
        self,
        kind: CompanyOperationKind,
        operation: Callable[[], _Result],
        executor: WorkerExecutor | None,
        *,
        document: Disclosure | None = None,
        source: str = "local",
    ) -> Future[_Result]:
        event = self.queued(kind, document=document, source=source)

        def execute() -> _Result:
            with self.running(event):
                return operation()

        return self._submit(event, execute, executor)

    def submit_pages(
        self,
        operation: Callable[[Callable[[int, int], None]], _Result],
        executor: WorkerExecutor | None,
        *,
        document: Disclosure,
    ) -> Future[_Result]:
        event = self.queued("ocr", document=document)

        def execute() -> _Result:
            with self.running(event) as page_progress:
                return operation(page_progress)

        return self._submit(event, execute, executor)

    def _submit(
        self, event: CompanyOperationProgress, execute: Callable[[], _Result],
        executor: WorkerExecutor | None,
    ) -> Future[_Result]:
        if executor is None:
            future: Future[_Result] = Future()
            try:
                future.set_result(execute())
            except (OSError, RuntimeError, ValueError) as exc:
                future.set_exception(exc)
            return future
        try:
            future = SubmitOrReject(executor, execute)
        except BaseException:
            self._emit(replace(event, state="failed", at=self._monotonic()))
            raise
        future.add_done_callback(lambda finished: self.cancelled(event, finished))
        return future

    def _emit(self, event: CompanyOperationProgress) -> None:
        if self._report is not None:
            self._report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, operation=event))


__all__ = ["CompanyOperations"]
