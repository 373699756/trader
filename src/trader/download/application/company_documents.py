"""Bounded document download, analysis, and caller-owned acceptance."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Literal, Protocol

from trader.download.application.company_progress import (
    CompanyProgressState, ReportCompanyProgress, MarkCompanyPublicationPending, PrepareCompanyStockProgress,
    CompanyCancelled,
)
from trader.download.application.company_failures import CompanySourceCircuit, CompanyFailureCode
from trader.download.domain.company_evidence import (
    BuildCompanyDocumentPolicies, CompanyDocumentExtraction, CompanyDocumentFailure, CompanyDocumentTask,
    CompanyDocumentWorkload, CompanyDocumentRouteForFailure, CompanyOperationKind, CompanySyncConfiguration,
    Disclosure, EvidenceProgressState, IsTransientCompanyDocumentFailure,
)
from trader.download.domain.download_failure import DownloadFailureContext
from trader.infra.workers import WorkerExecutor, WorkerResourceRejectedError

if TYPE_CHECKING:
    from trader.download.application.company_evidence import CompanyDisclosureSource, CompanyEvidenceDependencies


class CompanyDocumentContext(Protocol):
    @property
    def observed_at(self) -> datetime: ...
    @property
    def started(self) -> float: ...
    @property
    def configuration(self) -> CompanySyncConfiguration: ...


class CompanyDocumentRun:
    """Bounded network/parse work; only this caller commits results and retry state."""

    def __init__(
        self,
        dependencies: CompanyEvidenceDependencies,
        population: tuple[str, ...],
        state: CompanyProgressState,
        context: CompanyDocumentContext,
        *, publish: Callable[[], int | None],
    ) -> None:
        self._publish = publish
        configuration = context.configuration
        self.dependencies, self.population, self.observed_at = dependencies, population, context.observed_at
        self.state, self.configuration, self.started = state, configuration, context.started
        self.sources = dependencies.document_sources
        if configuration.document_workers == 1 and not self.sources:
            self.sources = (dependencies.disclosures,)
        if len(self.sources) != configuration.document_workers or len({id(source) for source in self.sources}) != len(
            self.sources
        ):
            raise ValueError("company_document_sources_invalid")
        if configuration.document_workers > 1 and dependencies.document_executor is None:
            raise ValueError("company_document_executor_required")
        self.executor = dependencies.document_executor
        self.failed_identities: set[tuple[str, str, str]] = set()
        self.policies = BuildCompanyDocumentPolicies(
            configuration,
            dependencies.ocr_identity,
            text_identity=dependencies.text_identity,
        )
        self.circuit = CompanySourceCircuit()
        self.stopped = False
        self.capacity_blocked = False
        self.completed = self.attempted = 0
        self.attempted_identities: set[tuple[str, str, str]] = set()
        self.elapsed = 0.0
        self.batch_started: float | None = None
        self.batch_sources: tuple[str, ...] = ()

    def now(self) -> datetime:
        return self.observed_at + timedelta(seconds=max(0, self.dependencies.monotonic() - self.started))

    def workload(self, *, include_attempted: bool = False) -> CompanyDocumentWorkload:
        return self.state.operations.call("inventory", partial(self.dependencies.index.document_workload,
            self.population,
            self.now(),
            self.policies,
            excluded=() if include_attempted else tuple(self.failed_identities),
        ), component="risk_documents", endpoint="sqlite:company_collection")

    def has_due(self) -> bool:
        if CompanyCancelled(self.dependencies) or self.capacity_blocked or not self.workload().due:
            return False
        if not self.stopped:
            return True
        tasks = self.dependencies.index.pending_documents(self.population, self.now(), limit=1, policies=self.policies,
                                                          excluded=tuple(self.failed_identities))
        return bool(tasks and self.sources[0].document_ready(tasks[0].disclosure))

    def run_batch(self, *, discovering: bool = False, discover: Callable[[], int | None] | None = None, local_only: bool = False) -> None:
        dependencies, index = self.dependencies, self.dependencies.index
        if self.capacity_blocked or CompanyCancelled(dependencies):
            return
        self.state.current_stock = None
        workload = self.workload()
        if CompanyCancelled(dependencies):
            return
        queue = deque(
            index.pending_documents(
                self.population,
                self.now(),
                limit=self.configuration.document_batch_size,
                policies=self.policies,
                excluded=tuple(self.failed_identities),
                local_only=local_only,
            )
        )
        self.batch_sources = tuple(sorted({task.disclosure.source for task in queue}))
        self.attempted_identities.clear()
        text_queue: deque[CompanyDocumentTask] = deque()
        ocr_queue: deque[CompanyDocumentTask] = deque()
        for task in tuple(queue):
            raw_ready, checked = self.state.operations.call_observed(
                "inventory", partial(self.sources[0].document_ready, task.disclosure), document=task.disclosure,
                component="risk_documents", endpoint="local:company_raw_files",
            )
            if raw_ready:
                self.state.operations.result(checked, "reused", component="risk_documents", original_source=task.disclosure.source)
                queue.remove(task)
                (ocr_queue if task.route == "ocr" else text_queue).append(task)
        if local_only or self.stopped:
            queue.clear()
        pending_documents: dict[Future[CompanyDocumentExtraction | None], tuple[int, Literal["download", "text", "ocr"], CompanyDocumentTask]] = {}
        available = deque(range(len(self.sources)))
        completed_before = self.completed
        self.batch_started = dependencies.monotonic()
        self.report(workload, completed_before)
        last_publish = dependencies.monotonic()
        unpublished = 0
        capacity_failure: str | None = None
        analysis_blocked = False
        while queue or text_queue or ocr_queue or pending_documents:
            if capacity_failure is not None:
                queue.clear()
                if analysis_blocked:
                    text_queue.clear()
                    ocr_queue.clear()
            urgent_publish = False
            stages: tuple[tuple[Literal["text", "ocr"], deque[CompanyDocumentTask], WorkerExecutor | None, int], ...] = (
                ("ocr", ocr_queue, dependencies.ocr_executor, self.configuration.ocr_workers),
                ("text", text_queue, dependencies.text_executor, self.configuration.text_workers),
            )
            for stage, ready, executor, capacity in stages:
                inflight = sum(value[1] == stage for value in pending_documents.values())
                while ready and inflight < capacity and not CompanyCancelled(dependencies):
                    task = ready.popleft()
                    if not index.document_required(task.disclosure):
                        continue
                    self._stage(task.disclosure, "analyzing", raw_saved=True)
                    pending_documents[self._submit(self.sources[0], task, stage, executor)] = (-1, stage, task)
                    inflight += 1
                    self._attempt(task.disclosure)
            while queue and available and not self.stopped and capacity_failure is None and not CompanyCancelled(dependencies):
                slot = available.popleft()
                task = queue.popleft()
                item = task.disclosure
                if not index.document_required(item):
                    available.append(slot)
                    continue
                PrepareCompanyStockProgress(dependencies, self.state, item.codes[0])
                self.report(workload, completed_before, discovering=discovering)
                if CompanyCancelled(dependencies):
                    break
                self._stage(item, "downloading")
                pending_documents[self._submit(self.sources[slot], task, "download", self.executor)] = (slot, "download", task)
                self._attempt(item)
            if self.stopped or CompanyCancelled(dependencies):
                queue.clear()
            if CompanyCancelled(dependencies):
                text_queue.clear()
                ocr_queue.clear()
                for future in pending_documents:
                    future.cancel()
            if not pending_documents:
                break
            if discover is not None and any(not future.done() for future in pending_documents) and not CompanyCancelled(dependencies):
                discover()
            done, _ = wait(pending_documents, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                slot, completed_stage, task = pending_documents.pop(future)
                if slot >= 0:
                    available.append(slot)
                if future.cancelled():
                    self.state.operations.discard_future(future)
                    continue
                timing = self.state.operations.take_future(future)
                try:
                    extraction = future.result()
                except WorkerResourceRejectedError:
                    raise
                except (OSError, RuntimeError, ValueError) as exc:
                    if isinstance(exc, CompanyDocumentFailure) and exc.reason == "disclosure_document_ocr_required":
                        ocr_queue.append(replace(task, route="ocr"))
                        self._stage(task.disclosure, "ocr_pending", raw_saved=True)
                        self.state.operations.result(timing, "ocr_queued")
                        self.state.operations.recover(exc, "ocr_queued", exc.reason, record=False)
                    elif str(exc) == "company_raw_capacity_exhausted":
                        self.state.operations.record_failure(exc)
                        capacity_failure = "company_raw_capacity_exhausted"
                        analysis_blocked = analysis_blocked or completed_stage != "download"
                        queue.clear()
                        self.report(workload, completed_before, discovering=discovering,
                                    failure_document=task.disclosure, failure_stage=completed_stage,
                                    failure_reason="company_raw_capacity_exhausted")
                        self.state.operations.recover(exc, "source_deferred", "company_raw_capacity_exhausted")
                    elif not CompanyCancelled(dependencies):
                        reason = self.failed(task.disclosure, exc, stage=completed_stage)
                        self.report(workload, completed_before, discovering=discovering,
                                    failure_document=task.disclosure, failure_stage=completed_stage, failure_reason=reason)
                else:
                    if completed_stage == "download":
                        self.circuit.success()
                        self.state.operations.result(timing, "downloaded")
                        self._stage(task.disclosure, "downloaded", raw_saved=True)
                        (ocr_queue if task.route == "ocr" else text_queue).append(task)
                    elif extraction is not None:
                        try:
                            accepted, committed = self.state.operations.call_observed(
                                "document_commit", partial(index.commit_document, task.disclosure, extraction, self.now()),
                                document=task.disclosure, raw_saved=True, text_extracted=True,
                                endpoint="sqlite:company_collection",
                            )
                        except RuntimeError as exc:
                            if str(exc) not in {"company_raw_capacity_exhausted", "company_temporary_space_exhausted"}:
                                raise
                            capacity_failure = str(exc)
                            analysis_blocked = True
                            self.state.operations.result(timing, "rejected", extraction=extraction)
                            self.state.operations.recover(exc, "source_deferred", capacity_failure)
                            self.report(workload, completed_before, discovering=discovering,
                                        failure_document=task.disclosure, failure_stage="document_commit",
                                        failure_reason=capacity_failure)
                            continue
                        self.state.operations.result(timing, "accepted" if accepted else "rejected",
                                                     commit=committed, extraction=extraction)
                        if accepted:
                            self.completed += 1
                            unpublished += 1
                            self.state.changes += 1
                            MarkCompanyPublicationPending(dependencies, self.state)
                            self.state.unpublished_documents += 1
                            if dependencies.coalesce_publications and not CompanyCancelled(dependencies):
                                urgent_publish = urgent_publish or index.document_publication_urgent(task.disclosure)
                self.report(workload, completed_before, discovering=discovering)
            pending_count = self.state.unpublished_documents if dependencies.coalesce_publications else unpublished
            pending_since = self.state.publication_pending_since if dependencies.coalesce_publications else last_publish
            publication_waiting = self.state.publication_required if dependencies.coalesce_publications else bool(unpublished)
            if publication_waiting and not CompanyCancelled(dependencies) and (
                urgent_publish
                or pending_count >= self.configuration.publication_batch_size
                or pending_since is not None and dependencies.monotonic() - pending_since >= self.configuration.publication_interval_seconds
            ):
                self._publish()
                unpublished = 0
                last_publish = dependencies.monotonic()
        if capacity_failure is not None:
            self.capacity_blocked = True
            self.state.failures += (f"risk_documents:{capacity_failure}",)
        self.elapsed += max(0.0, dependencies.monotonic() - self.batch_started)
        self.batch_started = None
        if CompanyCancelled(dependencies):
            return
        try:
            final_workload = self.workload()
        except InterruptedError:
            if CompanyCancelled(dependencies):
                return
            raise
        self.report(
            final_workload,
            self.completed,
            status="deferred" if self.stopped or self.capacity_blocked else "complete",
            discovering=discovering,
        )

    def _attempt(self, item: Disclosure) -> None:
        key = item.source, item.identity, item.digest
        if key not in self.attempted_identities:
            self.attempted_identities.add(key)
            self.attempted += 1

    def _stage(self, item: Disclosure, stage: str, *, raw_saved: bool | None = None) -> None:
        self.state.operations.call_observed(
            "collection_commit", partial(self.dependencies.index.document_stage, item, stage),
            document=item, component="risk_documents", endpoint=f"sqlite:document_stage.{stage}", raw_saved=raw_saved,
        )

    def _submit(
        self, source: CompanyDisclosureSource, task: CompanyDocumentTask, stage: Literal["download", "text", "ocr"], executor: WorkerExecutor | None
    ) -> Future[CompanyDocumentExtraction | None]:
        if stage == "ocr":
            return self.state.operations.submit_pages(
                lambda report: source.extract_document(task.disclosure, route="ocr", page_progress=report),
                executor, document=task.disclosure,
                timeout_seconds=self.configuration.document_ocr_timeout_seconds,
                endpoint=source.request_endpoint("ocr"),
            )
        function: Callable[[Disclosure], CompanyDocumentExtraction | None] = (
            source.download_document if stage == "download" else partial(source.extract_document, route=task.route)
        )
        return self.state.operations.submit(
            stage, partial(function, task.disclosure), executor, document=task.disclosure,
            timeout_seconds=(self.configuration.document_download_timeout_seconds if stage == "download"
                             else self.configuration.document_parse_timeout_seconds),
            raw_saved=True if stage != "download" else None,
            endpoint=source.request_endpoint(stage),
        )

    def failed(self, item: Disclosure, exc: BaseException, *, stage: str = "analysis") -> str:
        self.state.operations.record_failure(exc, fallback=DownloadFailureContext(
            "company_document", code=item.codes[0], supplier=item.source, operation_kind=stage,
            object_id=item.identity, revision=item.digest, title=item.title,
        ))
        self.failed_identities.add((item.source, item.identity, item.digest))
        failure = CompanyFailureCode(exc)
        if failure == "disclosure_document_ocr_timeout" and self.dependencies.index.document_ocr_timeouts(
            item, self.policies.ocr
        ) >= 1:
            failure = "disclosure_document_ocr_timeout_exhausted"
        self.state.failed_documents += 1
        self.state.failures = tuple(dict.fromkeys((*self.state.failures, f"risk_documents:{failure}")))
        now = self.now()
        route = CompanyDocumentRouteForFailure(failure)
        policy = self.policies.for_route(route)
        payload_hash = exc.payload_hash if isinstance(exc, CompanyDocumentFailure) else None
        retry_seconds = _document_retry_seconds(self.configuration, failure)
        self.state.operations.register_failure(exc, partial(self.dependencies.index.document_failure,
            item,
            policy,
            failure,
            now,
            now + timedelta(seconds=retry_seconds),
            payload_hash,
            exc.diagnostic if isinstance(exc, CompanyDocumentFailure) else None,
            self.policies.ocr,
        ))
        MarkCompanyPublicationPending(self.dependencies, self.state)
        if stage == "download" and self.circuit.failed(exc):
            self.stopped = True
            self.state.failures += ("risk_documents:source_stopped",)
        self.state.operations.recover(
            exc, "source_deferred" if self.stopped else "cooldown" if IsTransientCompanyDocumentFailure(failure)
            else "unextractable", failure, retry_at=now + timedelta(seconds=retry_seconds),
        )
        return failure

    def report(
        self,
        workload: CompanyDocumentWorkload,
        completed_before: int,
        *,
        status: EvidenceProgressState = "running",
        discovering: bool = False,
        failure_document: Disclosure | None = None,
        failure_stage: CompanyOperationKind | None = None,
        failure_reason: str | None = None,
    ) -> None:
        elapsed = self.elapsed + (
            max(0.0, self.dependencies.monotonic() - self.batch_started) if self.batch_started is not None else 0.0
        )
        rate = self.completed / elapsed if self.completed >= 5 and elapsed >= 1 else None
        due = max(0, workload.due - self.completed + completed_before)
        ReportCompanyProgress(
            self.dependencies,
            "risk_documents",
            self.state,
            max(0, workload.pending - self.completed + completed_before),
            completed=self.completed,
            attempted_documents=self.attempted,
            deferred_documents=workload.cooling,
            status=status,
            document_rate=rate,
            document_eta_seconds=due / rate if rate and due else None,
            ocr_documents=workload.ocr,
            unextractable_documents=workload.unextractable,
            next_document_retry_at=workload.next_retry_at,
            discovering_documents=discovering,
            unresolved_document_failures=workload.unresolved_failures,
            unpublished_documents=self.state.unpublished_documents,
            failure_document=failure_document,
            failure_stage=failure_stage,
            failure_reason=failure_reason,
            sources=self.batch_sources,
        )


def _document_retry_seconds(configuration: CompanySyncConfiguration, failure: str) -> float:
    return (
        configuration.document_transient_retry_seconds
        if IsTransientCompanyDocumentFailure(failure)
        else configuration.document_failure_retry_seconds
    )
