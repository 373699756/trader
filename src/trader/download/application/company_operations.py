"""Typed operation boundaries and explicit caller-owned company outcomes."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from threading import Lock
from typing import Literal, TypeVar, cast

from trader.download.application.download_failure import DownloadFailurePort
from trader.download.domain.company_evidence import (
    CompanyDocumentExtraction, CompanyOperationKind, CompanyOperationProgress, CompanyOperationResult,
    CompanyRecoveryAction, CompanyRecoveryDecision, CompanyResultOutcome, Disclosure, DisclosurePartition,
    EvidenceSyncProgress, StructuredCompanyQuery,
)
from trader.download.domain.download_failure import DownloadFailureContext
from trader.infra.workers import SubmitOrReject, WorkerExecutor

_Result = TypeVar("_Result")


@dataclass(frozen=True)
class CompanyOperationTiming:
    event: CompanyOperationProgress
    metadata: Disclosure | None
    scope: DisclosurePartition | StructuredCompanyQuery | None
    object_id: str
    queued_seconds: float
    call_seconds: float | None
    timeout_seconds: float | None
    raw_saved: bool | None = None
    text_extracted: bool | None = None
    component: str = ""
    endpoint: str = ""


@dataclass(frozen=True)
class _Invocation:
    event: CompanyOperationProgress
    metadata: Disclosure | None
    scope: DisclosurePartition | StructuredCompanyQuery | None
    object_id: str
    timeout_seconds: float | None
    completed: Future[CompanyOperationTiming]
    raw_saved: bool | None = None
    text_extracted: bool | None = None
    component: str = ""
    endpoint: str = ""


@dataclass
class _Failure:
    timing: CompanyOperationTiming
    reported: bool = False
    secondary: str = ""


class CompanyOperations:
    """Workers observe execution; only callers decide acceptance, recovery, and output."""

    def __init__(
        self,
        report: Callable[[EvidenceSyncProgress], None] | None,
        monotonic: Callable[[], float],
        names: tuple[tuple[str, str], ...],
        *,
        population: tuple[str, ...] = (),
        failures: DownloadFailurePort | None = None,
        run_id: str = "",
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._report, self._monotonic, self._names = report, monotonic, names
        self._codes = frozenset(population)
        self._lock = Lock()
        self._next = 0
        self._failures, self._run_id = failures, run_id
        self._cancel_requested = cancel_requested
        self._submitted: dict[Future[object], _Invocation] = {}
        self._failed: dict[BaseException, _Failure] = {}

    @property
    def run_id(self) -> str:
        return self._run_id

    def queued(
        self, kind: CompanyOperationKind, *, document: Disclosure | None = None,
        code: str = "", source: str = "local",
    ) -> CompanyOperationProgress:
        with self._lock:
            self._next += 1
            token = self._next
        if document is not None:
            code, source = document.codes[0], document.source
        event = CompanyOperationProgress(
            token, kind, "queued", self._monotonic(), code,
            next((name for candidate, name in self._names if candidate == code), ""),
            document.title if document is not None else "", source,
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

    def _prepare(
        self, kind: CompanyOperationKind, *, document: Disclosure | None = None,
        code: str = "", source: str = "local",
        scope: DisclosurePartition | StructuredCompanyQuery | None = None,
        object_id: str = "", timeout_seconds: float | None = None,
        raw_saved: bool | None = None, text_extracted: bool | None = None,
        component: str = "", endpoint: str = "",
    ) -> _Invocation:
        event = self.queued(kind, document=document, code=code, source=source)
        identity = object_id or (document.identity if document is not None else
            f"{scope.market}:{scope.start}:{scope.end}:{scope.page}:{scope.query.key if scope.query else 'all'}"
            if isinstance(scope, DisclosurePartition) else
            f"{scope.component}:{scope.start}:{scope.end}:{scope.page}" if scope is not None else code or kind)
        component = component or (scope.component if isinstance(scope, StructuredCompanyQuery) else
                                  "announcements" if isinstance(scope, DisclosurePartition) else
                                  "risk_documents" if document is not None else "")
        return _Invocation(event, document, scope, identity, timeout_seconds, Future(), raw_saved, text_extracted,
                           component, endpoint)

    def _execute(self, invocation: _Invocation, operation: Callable[[Callable[[int, int], None]], _Result]) -> _Result:
        started = self._monotonic()
        latest_pages: tuple[int, int] | None = None
        error: BaseException | None = None
        try:
            with self.running(invocation.event) as observe:
                def pages(completed: int, total: int) -> None:
                    nonlocal latest_pages
                    latest_pages = completed, total
                    observe(completed, total)
                return operation(pages)
        except BaseException as exc:
            error = exc
            raise
        finally:
            event = invocation.event
            if latest_pages is not None:
                event = replace(event, completed_units=latest_pages[0], total_units=latest_pages[1], unit="pages")
            timing = CompanyOperationTiming(
                event, invocation.metadata, invocation.scope, invocation.object_id,
                max(0.0, started - event.at), max(0.0, self._monotonic() - started),
                invocation.timeout_seconds, invocation.raw_saved, invocation.text_extracted,
                component=invocation.component, endpoint=invocation.endpoint,
            )
            invocation.completed.set_result(timing)
            if error is not None:
                with self._lock:
                    self._failed[error] = _Failure(timing)

    def call_observed(
        self, kind: CompanyOperationKind, operation: Callable[[], _Result], *,
        document: Disclosure | None = None, code: str = "", source: str = "local",
        scope: DisclosurePartition | StructuredCompanyQuery | None = None,
        object_id: str = "", timeout_seconds: float | None = None,
        raw_saved: bool | None = None, text_extracted: bool | None = None,
        component: str = "", endpoint: str = "",
    ) -> tuple[_Result, CompanyOperationTiming]:
        invocation = self._prepare(
            kind, document=document, code=code, source=source, scope=scope, object_id=object_id,
            timeout_seconds=timeout_seconds, raw_saved=raw_saved, text_extracted=text_extracted,
            component=component, endpoint=endpoint,
        )
        try:
            value = self._execute(invocation, lambda _pages: operation())
        except Exception as exc:
            if not isinstance(exc, InterruptedError):
                self.record_failure(exc)
            raise
        return value, invocation.completed.result()

    def call(
        self, kind: CompanyOperationKind, operation: Callable[[], _Result], *,
        document: Disclosure | None = None, code: str = "", source: str = "local",
        scope: DisclosurePartition | StructuredCompanyQuery | None = None,
        object_id: str = "", timeout_seconds: float | None = None,
        component: str = "", endpoint: str = "",
    ) -> _Result:
        return self.call_observed(
            kind, operation, document=document, code=code, source=source, scope=scope,
            object_id=object_id, timeout_seconds=timeout_seconds,
            component=component, endpoint=endpoint,
        )[0]

    def submit(
        self, kind: CompanyOperationKind, operation: Callable[[], _Result], executor: WorkerExecutor | None, *,
        document: Disclosure | None = None, source: str = "local",
        scope: DisclosurePartition | StructuredCompanyQuery | None = None,
        timeout_seconds: float | None = None, raw_saved: bool | None = None,
        component: str = "", endpoint: str = "",
    ) -> Future[_Result]:
        invocation = self._prepare(kind, document=document, source=source, scope=scope,
                                   timeout_seconds=timeout_seconds, raw_saved=raw_saved, component=component, endpoint=endpoint)
        return self._submit(invocation, lambda pages: operation(), executor)

    def submit_pages(
        self, operation: Callable[[Callable[[int, int], None]], _Result], executor: WorkerExecutor | None, *,
        document: Disclosure, timeout_seconds: float | None = None,
        endpoint: str = "",
    ) -> Future[_Result]:
        invocation = self._prepare("ocr", document=document, timeout_seconds=timeout_seconds, raw_saved=True, endpoint=endpoint)
        return self._submit(invocation, operation, executor)

    def _submit(
        self, invocation: _Invocation, operation: Callable[[Callable[[int, int], None]], _Result],
        executor: WorkerExecutor | None,
    ) -> Future[_Result]:
        def execute() -> _Result:
            return self._execute(invocation, operation)
        if executor is None:
            future: Future[_Result] = Future()
            try:
                future.set_result(execute())
            except (OSError, RuntimeError, ValueError) as exc:
                future.set_exception(exc)
        else:
            try:
                future = SubmitOrReject(executor, execute)
            except BaseException as exc:
                self._emit(replace(invocation.event, state="failed", at=self._monotonic()))
                timing = CompanyOperationTiming(invocation.event, invocation.metadata, invocation.scope,
                                                invocation.object_id, max(0.0, self._monotonic() - invocation.event.at),
                                                None, invocation.timeout_seconds, component=invocation.component,
                                                endpoint=invocation.endpoint)
                with self._lock:
                    self._failed[exc] = _Failure(timing)
                self.record_failure(exc)
                raise
            future.add_done_callback(lambda finished: self.cancelled(invocation.event, finished))
        with self._lock:
            self._submitted[cast(Future[object], future)] = invocation
        return future

    def take_future(self, future: Future[_Result]) -> CompanyOperationTiming:
        if not future.done() or future.cancelled():
            raise RuntimeError("company_operation_not_completed")
        with self._lock:
            invocation = self._submitted.pop(cast(Future[object], future))
        if not invocation.completed.done():
            raise RuntimeError("company_operation_observation_missing")
        return invocation.completed.result()

    def discard_future(self, future: Future[_Result]) -> None:
        with self._lock:
            self._submitted.pop(cast(Future[object], future), None)

    def cancelled(self, event: CompanyOperationProgress, future: Future[_Result]) -> None:
        if future.cancelled():
            self._emit(replace(event, state="cancelled", at=self._monotonic()))

    def result(
        self, timing: CompanyOperationTiming, outcome: CompanyResultOutcome, *,
        rows: int | None = None, commit: CompanyOperationTiming | None = None,
        extraction: CompanyDocumentExtraction | None = None,
        changed_rows: int | None = None, component: str = "",
        original_source: str = "",
    ) -> None:
        event, metadata = timing.event, timing.metadata
        result = CompanyOperationResult(
            event.token, event.kind, event.source, timing.object_id, outcome,
            run_id=self._run_id, code=event.code, name=event.name, related_codes=event.related_codes,
            metadata=metadata, revision=metadata.digest if metadata is not None else "", scope=timing.scope,
            rows=rows, queued_seconds=timing.queued_seconds, call_seconds=timing.call_seconds,
            commit_seconds=commit.call_seconds if commit is not None else None,
            commit_operation_id=commit.event.token if commit is not None else None,
            method=extraction.method if extraction is not None else None,
            text_chars=len(extraction.text) if extraction is not None else None,
            ocr_completed_pages=event.completed_units, ocr_total_pages=event.total_units,
            changed_rows=changed_rows, component=component or timing.component, endpoint=timing.endpoint,
            original_source=original_source,
        )
        if self._report is not None:
            self._report(EvidenceSyncProgress("result", 0, None, 0, 0, 0, result=result, run_id=self._run_id))

    def record_failure(
        self, exc: BaseException, *, timing: CompanyOperationTiming | None = None,
        fallback: DownloadFailureContext | None = None,
    ) -> None:
        if self._cancel_requested is not None and self._cancel_requested():
            return
        with self._lock:
            failure = self._failed.get(exc)
            if failure is None and timing is not None:
                failure = _Failure(timing)
                self._failed[exc] = failure
            if failure is not None and failure.reported:
                return
            if failure is not None:
                failure.reported = True
        if self._failures is None:
            return
        if failure is not None:
            event, scope = failure.timing.event, failure.timing.scope
            context = DownloadFailureContext(
                f"company_{event.kind}", code=event.code, supplier=event.source,
                operation_id=event.token, operation_kind=event.kind, object_id=failure.timing.object_id,
                revision=failure.timing.metadata.digest if failure.timing.metadata is not None else "",
                name=event.name, title=event.title,
                window_start=scope.start if scope is not None else None,
                window_end=scope.end if scope is not None else None,
                market=scope.market if isinstance(scope, DisclosurePartition) else "",
                page=scope.page if scope is not None else None,
                page_size=scope.page_size if isinstance(scope, DisclosurePartition) else None,
                queued_seconds=failure.timing.queued_seconds, elapsed_seconds=failure.timing.call_seconds,
                timeout_seconds=failure.timing.timeout_seconds,
                raw_saved=failure.timing.raw_saved, text_extracted=failure.timing.text_extracted,
                component=failure.timing.component, endpoint=failure.timing.endpoint,
                detail=(f"ocr_completed_pages={event.completed_units};ocr_total_pages={event.total_units}"
                        if event.unit == "pages" else ""),
            )
        elif fallback is not None:
            context = fallback
        else:
            return
        try:
            self._failures(context, exc)
        except Exception as secondary:
            if failure is not None:
                failure.secondary = type(secondary).__name__
            self.recover(exc, "recording_failed", type(exc).__name__, secondary=type(secondary).__name__,
                         record=False, consume=False, fallback=context)

    def recover(
        self, exc: BaseException, decision: CompanyRecoveryAction, reason: str, *,
        retry_at: datetime | None = None, secondary: str = "", record: bool = True, consume: bool = True,
        fallback: DownloadFailureContext | None = None,
    ) -> None:
        if record:
            self.record_failure(exc, fallback=fallback)
        with self._lock:
            failure = self._failed.pop(exc, None) if consume else self._failed.get(exc)
        context = fallback
        timing = failure.timing if failure is not None else None
        event = timing.event if timing is not None else None
        recovery = CompanyRecoveryDecision(
            event.token if event is not None else None,
            event.kind if event is not None else None,
            event.source if event is not None else context.supplier if context is not None else "local",
            timing.object_id if timing is not None else context.object_id if context is not None else "unknown",
            decision, reason, run_id=self._run_id,
            code=event.code if event is not None else context.code if context is not None else "",
            metadata=timing.metadata if timing is not None else None,
            revision=timing.metadata.digest if timing is not None and timing.metadata is not None else "",
            scope=timing.scope if timing is not None else None,
            retry_at=retry_at,
            secondary=secondary or (failure.secondary if failure is not None else ""),
            component=timing.component if timing is not None else context.component if context is not None else "",
            endpoint=timing.endpoint if timing is not None else context.endpoint if context is not None else "",
        )
        if self._report is not None:
            try:
                self._report(EvidenceSyncProgress("recovery", 0, None, 0, 0, 0, recovery=recovery, run_id=self._run_id))
            except Exception as reporting_error:
                if failure is not None:
                    failure.secondary = type(reporting_error).__name__

    def register_failure(self, exc: BaseException, registration: Callable[[], None]) -> None:
        """Keep the primary cause if its durable recovery receipt cannot be written."""
        self.record_failure(exc)
        primary_cause = exc.__cause__
        with self._lock:
            failure = self._failed.get(exc)
        timing = failure.timing if failure is not None else None
        try:
            self.call("collection_commit", registration,
                      document=timing.metadata if timing is not None else None,
                      scope=timing.scope if timing is not None else None,
                      source=timing.event.source if timing is not None else "local",
                      object_id=timing.object_id if timing is not None else "recovery_registration",
                      component=timing.component if timing is not None else "", endpoint="sqlite:company_recovery")
        except Exception as secondary:
            self.recover(exc, "recording_failed", type(exc).__name__, secondary=type(secondary).__name__,
                         consume=False)
            raise exc from primary_cause

    def _emit(self, event: CompanyOperationProgress) -> None:
        if self._report is not None:
            self._report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, operation=event, run_id=self._run_id))


__all__ = ["CompanyOperations", "CompanyOperationTiming"]
