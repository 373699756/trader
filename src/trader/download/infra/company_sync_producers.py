"""Bounded current-source producers; unavailable coverage never clears old facts."""

import hashlib
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta

from trader.download.application.company_sync import CompanyDirectoryResponseReceipt
from trader.download.application.company_failures import CompanyFailureCode
from trader.download.application.company_source_ports import (
    CompanyOfficialDocumentSource, CompanyRatioSource,
)
from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview, CompanyDocumentRiskRelevant
from trader.download.domain.company_evidence import (
    COMPANY_DOCUMENT_OCR_REQUIRED, CompanyDocumentExtraction, DocumentRoute,
    CompanyInputValidationFailure, CompanyDocumentFailure, CompanyDocumentPolicies, CompanySourceFailure, IsTransientCompanyDocumentFailure,
    CompanySyncConfiguration, CompanyDocumentRouteForFailure, Disclosure, DisclosurePartition, EvidenceMarket, StructuredCompanyQuery,
)
from datetime import datetime
from trader.download.domain.company_sync import (
    CompanyDisclosureDownloadProgress, CompanyDocumentDownloadProgress, CompanyDocumentDownloadTarget,
    CompanyDownloadIssue, CompanyDownloadOperation, CompanyPledgeSnapshotProgress,
    CompanySourceDownloadProgress, CompanyStructuredDownloadProgress,
    CompanySyncFamily, CompanySyncPhasePlan, CompanySyncPhaseState, CompanySyncRoundScope, CompanySyncStepResult,
)
from trader.download.domain.company_unlock_notice import IsCompanyUnlockListingNotice, IsCompanyUnlockListingRevision
from trader.download.infra.company_disclosure_source import DecodeCninfoDirectoryResponse
from trader.download.infra.company_disclosure_queries import DISCLOSURE_PAGE_DRIFT
from trader.download.infra.company_fact_inputs import CompanyCoverageComplete, DecodeCompanyDisclosure
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.download.infra.company_field_recovery import ReadCompanyFieldRecovery, ReadRequiredCompanyMaterialDependencies
from trader.download.infra.company_material_index import SQLiteCompanyMaterialIndex
from trader.download.infra.company_raw_files import CompanyRawCapacityError
from trader.download.infra.company_sync_inputs import CompanyRecoveredInput, SQLiteCompanySyncInputStore
from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.infra.http_contracts import HttpResponse
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from pathlib import Path
from trader.infra.sqlite_policy import SqliteIoPolicy


class CompanyRatioProducer:
    def __init__(
        self,
        inputs: SQLiteCompanySyncInputStore,
        source: CompanyRatioSource,
        query: StructuredCompanyQuery,
        *,
        current_codes: tuple[str, ...] | None = None,
        observe_download: Callable[[CompanySourceDownloadProgress], None] | None = None,
    ) -> None:
        if query.component not in {"pledge", "unlock"}:
            raise ValueError("company_ratio_producer_component_invalid")
        self._inputs, self._source, self._query = inputs, source, query
        self._current_codes = current_codes
        self._observe_download = observe_download
        self._plan = CompanySyncPhasePlan(CompanySyncFamily(query.component), CanonicalDigest(query), (
            f"{query.component}_total_share_denominator_unverified", f"{query.component}_population_coverage_unverified",
        ))

    @property
    def plan(self) -> CompanySyncPhasePlan:
        return self._plan

    def state(self, scope: CompanySyncRoundScope) -> CompanySyncPhaseState:
        day = self._inputs.pledge_day(scope.identity) if self._query.component == "pledge" else None
        if self._query.component == "pledge" and day is None:
            return CompanySyncPhaseState(CanonicalDigest("pledge_snapshot_day"), self._current_codes != ())
        query = replace(self._query, start=day, end=day) if day is not None else self._query
        cursor = self._inputs.ratio_cursor(scope.identity, query, scope.cutoff)
        return CompanySyncPhaseState(CanonicalDigest((query, cursor)), cursor is not None and self._current_codes != ())

    def advance(self, scope: CompanySyncRoundScope, *, cancel_requested: Callable[[], bool], deadline: float) -> CompanySyncStepResult:
        if self._current_codes == ():
            return CompanySyncStepResult(True, False, gaps=("population_members_became_ineligible",), pending_count=None)
        day = self._inputs.pledge_day(scope.identity) if self._query.component == "pledge" else None
        if self._query.component == "pledge" and day is None:
            if self._observe_download is not None:
                self._observe_download(CompanyDownloadOperation(self._plan.family, "pledge_snapshot", self._query))
            day = self._source.pledge_day(scope.cutoff)
            self._inputs.freeze_pledge_day(scope.identity, day, scope.cutoff)
            if self._observe_download is not None:
                self._observe_download(CompanyPledgeSnapshotProgress(day))
            return CompanySyncStepResult(False, True, accepted_inputs=1, source_calls=1)
        query = replace(self._query, start=day, end=day) if day is not None else self._query
        cursor = self._inputs.ratio_cursor(scope.identity, query, scope.cutoff)
        if cursor is not None:
            if self._observe_download is not None:
                self._observe_download(CompanyDownloadOperation(self._plan.family, "structured_page", cursor))
            page = self._source.fetch(cursor, observed_at=scope.cutoff)
            self._inputs.accept_ratios(scope.identity, cursor, page, scope.cutoff, eligible_codes=self._current_codes)
            if self._observe_download is not None:
                self._observe_download(CompanyStructuredDownloadProgress(
                    family=self._plan.family,
                    accepted_pages=page.page,
                    expected_pages=page.pages,
                    accepted_rows=cursor.received_rows + page.row_count,
                    expected_rows=page.total,
                ))
            return CompanySyncStepResult(False, True, accepted_inputs=len(page.ratios), source_calls=1)
        # These decoded values lack a proven total-share denominator and
        # absence/coverage contract. Retain them as observations, not facts.
        return CompanySyncStepResult(True, False, pending_count=0)


@dataclass(frozen=True, slots=True)
class _RequiredDocument:
    disclosure: Disclosure
    body_hash: bytes
    payload_hash: bytes
    for_recommendation: bool


@dataclass(frozen=True, slots=True)
class _NewDocument:
    disclosure: Disclosure
    before_partition: DisclosurePartition | None


class CompanyAnnouncementProducer:
    def __init__(self, inputs: SQLiteCompanySyncInputStore, material: SQLiteCompanyMaterialIndex,
        source: CompanyOfficialDocumentSource, scope: CompanySyncRoundScope, *, sqlite_policy: SqliteIoPolicy, extraction_hash: bytes,
        io_budget: CompanySyncIoBudget | None = None, current_codes: tuple[str, ...] | None = None,
        configuration: CompanySyncConfiguration | None = None, policies: CompanyDocumentPolicies | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        observe_download: Callable[[CompanySourceDownloadProgress], None] | None = None,
        latest_only: bool = False,
    ) -> None:
        self._inputs, self._material, self._source, self._sqlite_policy = inputs, material, source, sqlite_policy
        self._extraction_hash = extraction_hash
        self._current_codes = scope.codes if current_codes is None else current_codes
        self._io_budget = io_budget
        self._configuration, self._policies, self._wall_clock = configuration, policies, wall_clock
        self._next_retry: datetime | None = None
        self._observe_download = observe_download
        self._latest_only = latest_only
        self._latest_windows: tuple[DisclosurePartition, ...] = ()
        self._latest_coverage_recorded = False
        self._unread_latest_document = False
        markets = tuple(sorted({EvidenceMarket(code) for code in scope.codes}))
        self._partitions = tuple(DisclosurePartition(market, start, end)
            for start, end in ((scope.cutoff.date(), scope.cutoff.date()),
                (scope.cutoff.date() - timedelta(days=365), scope.cutoff.date() - timedelta(days=1)))
            for market in markets)
        self._plan = CompanySyncPhasePlan(CompanySyncFamily.ANNOUNCEMENTS, CanonicalDigest((self._partitions, policies, extraction_hash.hex())), (
            "persistent_risk_initial_state_unverified", "issuer_revision_relationship_unverified",
        ))

    @contextmanager
    def _session(self, path: Path) -> Iterator[SqliteConnection]:
        if self._io_budget is not None:
            with self._io_budget.session(path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
                yield connection
        else:
            with SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=self._sqlite_policy) as connection:
                yield connection

    @property
    def plan(self) -> CompanySyncPhasePlan:
        return self._plan

    def _emit_download(self, progress: CompanySourceDownloadProgress) -> None:
        if self._observe_download is not None:
            self._observe_download(progress)

    def freeze_latest_window(self, scope: CompanySyncRoundScope) -> None:
        if self._latest_only and not self._latest_windows:
            markets = tuple(sorted({EvidenceMarket(code) for code in scope.codes}))
            self._latest_windows = self._inputs.freeze_latest_announcement_windows(scope.identity, scope.cutoff, markets)

    def prepare_latest_coverage(self, scope: CompanySyncRoundScope) -> None:
        if not self._latest_only:
            return
        self.freeze_latest_window(scope)
        if not self._initialized(scope):
            for window in self._latest_windows:
                if window.start < window.end:
                    self._material.recheck_prior_intraday((window.market,), window.end, since=window.start)

    def latest_inputs_complete(self, scope: CompanySyncRoundScope) -> bool:
        return self.latest_inputs_settled(scope) and not self._unread_latest_document

    def latest_inputs_settled(self, scope: CompanySyncRoundScope) -> bool:
        """A later cutoff may proceed when only a recorded unavailable original remains."""
        if not self._latest_only or not self._initialized(scope):
            return False
        self.freeze_latest_window(scope)
        if self._work(scope) is not None or self._next_retry is not None:
            return False
        with self._session(self._material.path) as connection:
            for w in self._latest_windows:
                ranges = connection.run("""SELECT start_date,end_date FROM coverage WHERE market=?
                    AND end_date>=? AND start_date<=? AND (end_date<? OR checked_at>=?) ORDER BY start_date,end_date""",
                    (w.market, w.start.isoformat(), w.end.isoformat(), w.end.isoformat(), scope.cutoff.isoformat())).fetchall()
                if not CompanyCoverageComplete(ranges, w.start, w.end):
                    return False
        if not self._latest_coverage_recorded:
            for window in self._latest_windows:
                self._inputs.complete_task(scope.identity, f"latest_announcement_checked:{window.market}",
                    scope.cutoff.isoformat(), 0, CanonicalDigest((window.market, scope.cutoff)))
            self._latest_coverage_recorded = True
        return True

    def _initialized(self, scope: CompanySyncRoundScope) -> bool:
        task = "initialize_latest" if self._latest_only else "initialize"
        return self._inputs.task_done(scope.identity, "announcements", task)

    def _cooling(self, failure: str, retry_at: datetime, *, directory: bool = False) -> bool:
        if self._wall_clock is None:
            return False
        if retry_at <= self._wall_clock():
            return False
        if (directory or IsTransientCompanyDocumentFailure(failure)) and failure != "access_denied":
            self._next_retry = min(self._next_retry or retry_at, retry_at)
        return True

    def _partition(self, scope: CompanySyncRoundScope) -> DisclosurePartition | None:
        windows = self._latest_windows if self._latest_only else self._partitions
        parents = (parent for window in windows for parent in self._material.pending_partitions(window,
            include_left_edge=not self._latest_only and window.start < window.end))
        for parent in sorted(parents, key=lambda item: item.end, reverse=True):
            queries = self._material.disclosure_queries(parent)
            # After the last child, confirm the parent against the complete union.
            candidates: tuple[DisclosurePartition, ...]
            if queries is None:
                candidates = (parent,)
            elif queries:
                candidates = queries
            else:
                candidates = (replace(parent, page=1),)
            for pending in candidates:
                if self._wall_clock is not None:
                    drift_retry_by = (self._wall_clock() + timedelta(seconds=self._configuration.request_interval_seconds)
                                      if self._configuration is not None else None)
                    retry = self._material.partition_retry(pending, drift_retry_by=drift_retry_by)
                    if retry is not None and self._cooling(retry.failure, retry.retry_at, directory=True):
                        continue
                if not self._inputs.task_done(scope.identity, "announcements", "ineligible_partition:" + CanonicalDigest(pending)):
                    return pending
        return None

    def _refine_partition(self, partition: DisclosurePartition, scope: CompanySyncRoundScope) -> None:
        if partition.query is None and partition.start < partition.end:
            self._material.split_partition(partition, scope.cutoff)
        else:
            # Reuse the source's board/issuer split and the material owner's
            # exact union reconciliation; never deduplicate into coverage.
            self._material.split_query(partition, self._source.split_queries(partition))

    def _document(self, scope: CompanySyncRoundScope) -> _RequiredDocument | None:
        if self._configuration is None or self._policies is None or self._wall_clock is None:
            return None
        deferred: _RequiredDocument | None = None
        with self._session(self._material.path) as connection, SqliteTransaction(connection):
            dependencies = ReadRequiredCompanyMaterialDependencies(connection, self._extraction_hash)
            if not dependencies:
                return None
            if len(dependencies) > 120000:
                raise ValueError("company_required_material_scope_limit")
            frozen_codes = frozenset(scope.codes)
            dependencies = tuple(item for item in dependencies if item.code in frozen_codes)
            if not dependencies:
                return None
            dependency_codes = tuple(sorted({item.code for item in dependencies}))
            with self._session(self._material.facts_path) as formal, SqliteTransaction(formal):
                recoveries = ReadCompanyFieldRecovery(formal, connection, dependency_codes, scope.cutoff, self._material.raw_root, field_rule_hash=CompanyFieldProofRulesHash())
                valid = frozenset((item.code, item.source, item.evidence_id, item.record_hash, item.body_hash) for item in dependencies
                    if formal.run("SELECT 1 FROM company_input_receipt WHERE code=? AND source=? AND source_key=? AND record_hash=? AND body_hash=? AND extraction_hash=? AND outcome='valid'",
                    (item.code, item.source, item.evidence_id, item.record_hash, item.body_hash, self._extraction_hash)).fetchone() is not None)
            permitted = frozenset((item.gap.code, item.gap.evidence_id) for item in recoveries if item.action in {"download", "extract", "ocr"})
            for dependency in dependencies:
                if ((dependency.code, dependency.evidence_id) not in permitted
                    or (dependency.code, dependency.source, dependency.evidence_id, dependency.record_hash, dependency.body_hash) not in valid
                    or dependency.record_hash != dependency.current_record_hash
                    or dependency.current_body_hash not in (None, dependency.body_hash)
                    or dependency.extraction_hash != self._extraction_hash):
                    continue
                row = connection.run("""SELECT d.*,c.code,x.payload_hash FROM disclosures d
                    JOIN disclosure_codes c ON c.disclosure_id=d.id
                    JOIN document_extractions x ON x.disclosure_id=d.id AND x.record_hash=d.record_hash
                    WHERE c.code=? AND d.source=? AND d.source_key=? AND d.record_hash=? AND d.body_retained=0
                    AND d.published_at>=? AND d.published_at<=? AND (SELECT COUNT(*) FROM disclosure_codes members WHERE members.disclosure_id=d.id)=1""",
                    (dependency.code, dependency.source, dependency.evidence_id, dependency.record_hash,
                     (scope.cutoff.date() - timedelta(days=365)).isoformat(), scope.cutoff.isoformat())).fetchone()
                if row is None or not isinstance(row["payload_hash"], bytes) or len(row["payload_hash"]) != 32:
                    continue
                item = DecodeCompanyDisclosure(row)
                if self._latest_only and all(item.published_at.date() < w.start for w in self._latest_windows
                                             if w.market == EvidenceMarket(dependency.code)) and self._source.document_payload_hash(item) is None:
                    continue
                retry = self._material.document_retry(item, self._policies.standard)
                if retry is not None and retry.failure != COMPANY_DOCUMENT_OCR_REQUIRED and self._cooling(retry.failure, retry.retry_after):
                    self._unread_latest_document = True
                    continue
                required = _RequiredDocument(
                    item, dependency.body_hash, row["payload_hash"],
                    dependency.scope in {"risk", "financial"} or _risk_priority(item.title),
                )
                if (not self._inputs.task_done(scope.identity, "announcements", "document:" + _required_identity(required))
                    and not self._inputs.task_done(scope.identity, "announcements", "ineligible_document:" + _required_identity(required))):
                    if required.for_recommendation:
                        return required
                    if deferred is None:
                        deferred = required
        return deferred

    def _work(self, scope: CompanySyncRoundScope) -> _RequiredDocument | _NewDocument | DisclosurePartition | None:
        self._next_retry = None
        self._unread_latest_document = False
        self.freeze_latest_window(scope)
        required = self._document(scope)
        if required is not None and required.for_recommendation:
            return required
        partition = self._partition(scope)
        turn = self._inputs.announcement_turn(scope.identity, CanonicalDigest(partition)) if partition is not None else "risk"
        if turn == "directory":
            return partition
        fresh = self._new_document(scope, priority_only=partition is not None or required is not None, prefer_unlock=turn == "unlock")
        if fresh is not None:
            return _NewDocument(fresh, partition)
        return partition if partition is not None else required

    def state(self, scope: CompanySyncRoundScope) -> CompanySyncPhaseState:
        if not self._initialized(scope):
            return CompanySyncPhaseState(CanonicalDigest("initialize"), False)
        work = self._work(scope)
        if isinstance(work, _RequiredDocument):
            payload_hash = self._source.document_payload_hash(work.disclosure)
            can_download = work.disclosure.codes[0] in self._current_codes and payload_hash is None
            return CompanySyncPhaseState(
                _required_identity(work), can_download, local_input_verified=payload_hash == work.payload_hash,
            )
        if isinstance(work, _NewDocument):
            ready = self._source.document_payload_hash(work.disclosure) is not None
            return CompanySyncPhaseState(CanonicalDigest((work.disclosure, self._policies)), not ready, local_input_verified=ready)
        if isinstance(work, DisclosurePartition):
            return CompanySyncPhaseState(CanonicalDigest(work), any(EvidenceMarket(code) == work.market for code in self._current_codes))
        return CompanySyncPhaseState(_required_identity(None), False)

    def recover_directory(
        self, scope: CompanySyncRoundScope, receipt: CompanyDirectoryResponseReceipt,
    ) -> CompanySyncStepResult | None:
        if not (self._inputs.task_done(scope.identity, "announcements", "initialize")
                or self._inputs.task_done(scope.identity, "announcements", "initialize_latest")):
            return None
        restored = DecodeCninfoDirectoryResponse(
            receipt.request_body, HttpResponse(200, receipt.response_content, encoding=receipt.encoding),
        )
        if restored is None:
            return None
        partition, page = restored
        if (
            not any(window.market == partition.market and window.start <= partition.end <= window.end
                    and (window.start <= partition.start or window.start < window.end) for window in self._partitions)
            or not any(EvidenceMarket(code) == partition.market for code in self._current_codes)
        ):
            return None
        self._emit_download(CompanyDownloadOperation(self._plan.family, "announcement_recover", partition))
        if not self._material.recover_directory_page(partition, page, scope.cutoff, receipt.state_hash):
            return None
        if self._observe_download is not None:
            # The saved request carries the page number but only the material
            # index knows the accepted prefix of an intermediate recovered page.
            accepted_rows = page.total if not page.has_more else len(page.rows) if partition.page == 1 else None
            self._observe_download(CompanyDisclosureDownloadProgress(partition, "recovered", accepted_rows, page.total))
        return CompanySyncStepResult(False, True, accepted_inputs=len(page.rows))

    def advance(self, scope: CompanySyncRoundScope, *, cancel_requested: Callable[[], bool], deadline: float) -> CompanySyncStepResult:
        if not self._initialized(scope):
            self.prepare_latest_coverage(scope)
            windows = self._partitions
            task, other_task = "initialize", "initialize_latest"
            if self._latest_only:
                task, other_task = other_task, task
                today = scope.cutoff.date()
                windows = (tuple(DisclosurePartition(w.market, today, today) for w in self._latest_windows)
                           + tuple(DisclosurePartition(w.market, w.start, today - timedelta(days=1))
                                   for w in self._latest_windows if w.start < today))
            other_initialized = self._inputs.task_done(scope.identity, "announcements", other_task)
            for initial_partition in windows:
                self._material.register_partition(initial_partition, scope.cutoff, retain_started_edge=not self._latest_only)
                if initial_partition.start == initial_partition.end == scope.cutoff.date() and not other_initialized:
                    self._material.restart_partition(initial_partition)
            self._inputs.complete_task(scope.identity, "announcements", task, 0, CanonicalDigest(windows))
            return CompanySyncStepResult(False, True)
        work = self._work(scope)
        if isinstance(work, _NewDocument):
            result = self._accept_document(scope, work)
            if work.before_partition is not None:
                self._inputs.complete_announcement_turn(scope.identity, CanonicalDigest(work.before_partition),
                    chose_unlock=_supplemental_unlock(work.disclosure.title))
            return result
        if isinstance(work, DisclosurePartition):
            partition = work
            if not any(EvidenceMarket(code) == partition.market for code in self._current_codes):
                self._inputs.complete_task(scope.identity, "announcements", "ineligible_partition:" + CanonicalDigest(partition), 0, CanonicalDigest(("ineligible", partition)))
                return CompanySyncStepResult(False, True, gaps=("population_members_became_ineligible",))
            operation = CompanyDownloadOperation(self._plan.family, "announcement_page", partition)
            self._emit_download(operation)
            try:
                page = self._source.fetch(partition, order="desc")
            except CompanySourceFailure as exc:
                code = CompanyFailureCode(exc)
                if (self._wall_clock is not None and self._configuration is not None
                    and code != "access_denied" and IsTransientCompanyDocumentFailure(code)):
                    retry_at = self._wall_clock() + timedelta(seconds=self._configuration.document_transient_retry_seconds)
                    self._material.partition_failure(partition, code)
                    self._material.defer_partition(partition, retry_at)
                    self._emit_download(CompanyDownloadIssue(operation, code, material_accepted=False, action="defer", retry_at=retry_at))
                    return CompanySyncStepResult(False, True, failure=code, gaps=("announcement_directory_retry",))
                raise
            if partition.query is None and self._material.disclosure_queries(partition) == ():
                complete = self._material.commit_query_coverage(partition, page, scope.cutoff)
                if complete:
                    self._emit_download(CompanyDisclosureDownloadProgress(partition, "reconciled", page.total, page.total))
                else:
                    self._emit_download(CompanyDownloadIssue(operation, "cninfo_query_coverage_mismatch", material_accepted=True, action="retry"))
                    self._emit_download(CompanyDisclosureDownloadProgress(partition, "accepted", len(page.rows), page.total))
                return CompanySyncStepResult(False, True, accepted_inputs=0 if complete else len(page.rows), source_calls=1)
            if page.total > 100 * partition.page_size:
                self._refine_partition(partition, scope)
                self._emit_download(CompanyDownloadIssue(
                    operation, "cninfo_partition_split_page_limit", material_accepted=False, action="split",
                ))
                self._emit_download(CompanyDisclosureDownloadProgress(partition, "split", None, page.total))
                return CompanySyncStepResult(False, True, source_calls=1)
            try:
                self._material.commit_page(partition, page, scope.cutoff)
            except CompanyInputValidationFailure as exc:
                if str(exc) not in DISCLOSURE_PAGE_DRIFT:
                    raise
                if (partition.query is None and page.total <= 100 * partition.page_size
                    and self._material.disclosure_queries(partition, include_completed=True)):
                    # commit_page already reopened this parent's first page.
                    # Keep the accepted child inventory while the live supplier
                    # is changing, and let the other market run during cooldown.
                    recheck_at = None
                    if self._wall_clock is not None and self._configuration is not None:
                        recheck_at = self._wall_clock() + timedelta(seconds=self._configuration.request_interval_seconds)
                        self._material.partition_failure(partition, str(exc))
                        self._material.defer_partition(partition, recheck_at)
                    self._emit_download(CompanyDownloadIssue(operation, str(exc), material_accepted=False,
                                                             action="retry", retry_at=recheck_at))
                    return CompanySyncStepResult(False, True, source_calls=1)
                # A drifting broad range cannot establish coverage. Refine
                # its durable owner instead of repeating the rejected page.
                self._refine_partition(partition, scope)
                self._emit_download(CompanyDownloadIssue(operation, str(exc), material_accepted=False, action="split"))
                self._emit_download(CompanyDisclosureDownloadProgress(partition, "split", None, page.total))
                return CompanySyncStepResult(False, True, source_calls=1)
            self._emit_download(CompanyDisclosureDownloadProgress(
                partition, "accepted", partition.received_rows + len(page.rows), page.total,
            ))
            return CompanySyncStepResult(False, True, len(page.rows), 1)
        document = work
        if document is None:
            if self._next_retry is not None and self._wall_clock is not None:
                delay = max(0.0, (self._next_retry - self._wall_clock()).total_seconds())
                return CompanySyncStepResult(False, True, gaps=("announcement_retry_pending",), deferred=True,
                    retry_after_seconds=delay)
            if self._latest_only:
                # The annual cursor can still have work once today's inputs are exhausted.
                gap = "latest_announcement_original_unverified" if self._unread_latest_document else "announcement_required_material_not_fully_verified"
                return CompanySyncStepResult(False, True, deferred=True, gaps=(gap,))
            return CompanySyncStepResult(True, False, pending_count=None, gaps=("announcement_required_material_not_fully_verified",))
        if document.disclosure.codes[0] not in self._current_codes:
            self._inputs.complete_task(scope.identity, "announcements", "ineligible_document:" + _required_identity(document), 0, _required_identity(document))
            return CompanySyncStepResult(False, True, gaps=("population_members_became_ineligible",))
        return self._accept_document(scope, document)

    def _recover_document(
        self, scope: CompanySyncRoundScope, document: _RequiredDocument, extraction: CompanyDocumentExtraction,
    ) -> str | None:
        item = document.disclosure
        if extraction.payload_hash != document.payload_hash or hashlib.sha256(extraction.text.encode()).digest() != document.body_hash:
            self._inputs.complete_task(scope.identity, "announcements", "document:" + _required_identity(document), 0, CanonicalDigest(extraction.payload_hash.hex()))
            return "required_original_hash_mismatch"
        if not self._material.recover_required_text(item.identity, extraction.text, scope.cutoff):
            return "required_original_not_accepted"
        self._inputs.record_recovered(scope.identity, CompanyRecoveredInput(item.codes[0], item.source,
            item.identity, item.digest, document.body_hash))
        self._inputs.complete_task(scope.identity, "announcements", "document:" + _required_identity(document), 1,
            CanonicalDigest((_required_identity(document), extraction.payload_hash.hex())))
        return None

    def _new_document(self, scope: CompanySyncRoundScope, *, priority_only: bool, prefer_unlock: bool) -> Disclosure | None:
        if self._configuration is None or self._policies is None or self._wall_clock is None:
            return None
        if self._wall_clock().utcoffset() is None:
            raise ValueError("company_document_retry_clock_invalid")
        current_codes = frozenset(scope.codes).intersection(self._current_codes)
        if not current_codes:
            return None
        alternate: Disclosure | None = None
        other: Disclosure | None = None
        today = scope.cutoff.date()
        scope_id = scope.identity
        unlock_window = (today.isoformat(), (today + timedelta(days=180)).isoformat())
        date_only_cutoff = scope.cutoff.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self._session(self._material.path) as connection:
            starts = {w.market: w.start.isoformat() for w in self._latest_windows}
            revision = """EXISTS(SELECT 1 FROM company_material_dependencies m WHERE m.disclosure_id=d.id
                AND m.code=c.code AND m.record_hash<>d.record_hash)"""
            projection = revision if self._latest_only else "0"
            selection = "AND (d.published_at>=? OR revised)" if self._latest_only else ""
            bounds = (min(starts.values()),) if self._latest_only else ()
            rows = connection.run(f"""SELECT d.*,c.code,retry.failure AS retry_failure,retry.retry_at,{projection} AS revised FROM disclosures d
                JOIN disclosure_codes c ON c.disclosure_id=d.id
                JOIN company_document_requirements required ON required.disclosure_id=d.id
                LEFT JOIN document_retries retry ON retry.disclosure_id=d.id AND retry.record_hash=d.record_hash AND retry.retry_policy_id=?
                WHERE d.source='cninfo' AND d.kind='announcement' AND d.body_checked=0
                AND d.published_at>=? AND d.published_at<=?
                {selection}
                AND (SELECT COUNT(*) FROM disclosure_codes members WHERE members.disclosure_id=d.id)=1
                ORDER BY d.published_at DESC,d.source_key""",
                (self._policies.standard, (scope.cutoff.date() - timedelta(days=365)).isoformat(), scope.cutoff.isoformat(), *bounds))
            for row in rows:
                if row["code"] not in current_codes:
                    continue
                if self._latest_only and row["published_at"] < starts[EvidenceMarket(row["code"])] and not row["revised"]:
                    continue
                title = row["title"]
                is_risk = _risk_priority(title)
                is_unlock = _supplemental_unlock(title)
                if not is_risk and not is_unlock and (priority_only or other is not None):
                    continue
                if is_unlock:
                    if row["published_at"] == date_only_cutoff or connection.run("""SELECT 1 FROM company_ratio_observation
                        WHERE scope=? AND component='unlock' AND code=? AND day BETWEEN ? AND ? AND ratio>0 LIMIT 1""",
                        (scope_id, row["code"], *unlock_window)).fetchone() is None:
                        continue
                if IsCompanyUnlockListingRevision(title) and not CompanyDocumentNeedsReview(title):
                    continue
                item = DecodeCompanyDisclosure(row)
                failure = row["retry_failure"]
                if failure and failure != COMPANY_DOCUMENT_OCR_REQUIRED:
                    if self._cooling(failure, datetime.fromisoformat(row["retry_at"])) or (
                        (failure == "access_denied" or not IsTransientCompanyDocumentFailure(failure))
                        and self._inputs.task_done(scope.identity, "new_announcement_document", CanonicalDigest((item, self._policies)))
                    ):
                        self._unread_latest_document = True
                        continue
                if is_unlock or is_risk:
                    if is_unlock == prefer_unlock:
                        return item
                    if alternate is None:
                        alternate = item
                elif other is None:
                    other = item
        return alternate or other

    def _accept_document(
        self, scope: CompanySyncRoundScope, work: _NewDocument | _RequiredDocument,
    ) -> CompanySyncStepResult:
        assert self._configuration is not None and self._policies is not None and self._wall_clock is not None
        item = work.disclosure
        called = not self._source.document_ready(item)
        target = CompanyDocumentDownloadTarget(item.codes[0], item.source, item.identity)
        operation = CompanyDownloadOperation(self._plan.family, "document_extract", target, route="standard")
        try:
            if called:
                operation = replace(operation, kind="document_download", route=None)
                self._emit_download(operation)
                self._download_document(item, target, recovered=isinstance(work, _RequiredDocument))
                operation = replace(operation, kind="document_extract", route="standard")
            route: DocumentRoute = "standard"
            while True:
                self._emit_download(operation)
                try:
                    extraction = self._source.extract_document(item, route=route)
                    break
                except CompanyDocumentFailure as failure:
                    next_route = CompanyDocumentRouteForFailure(failure.reason)
                    if next_route == "standard" or next_route == route or route == "ocr":
                        raise
                    self._emit_download(CompanyDownloadIssue(operation, failure.reason, material_accepted=False, action="retry"))
                    route = next_route
                    operation = replace(operation, route=route)
            if isinstance(work, _RequiredDocument):
                rejection = self._recover_document(scope, work, extraction)
                if rejection is not None:
                    self._emit_download(CompanyDownloadIssue(operation, rejection, material_accepted=False, action="stop"))
                    return CompanySyncStepResult(False, False, gaps=(rejection,), failure=rejection)
            elif not self._material.commit_document(item, extraction, scope.cutoff):
                raise ValueError("company_announcement_material_changed")
        except (CompanyDocumentFailure, CompanySourceFailure) as failure:
            if isinstance(work, _RequiredDocument):
                return self._document_failure(item, operation, failure, called, "required_original_recovery_failed")
            code = CompanyFailureCode(failure)
            if code == "access_denied" or not IsTransientCompanyDocumentFailure(code):
                key = CanonicalDigest((item, self._policies))
                if not self._inputs.task_done(scope.identity, "new_announcement_document", key):
                    self._inputs.complete_task(scope.identity, "new_announcement_document", key, 0, CanonicalDigest(code))
            return self._document_failure(item, operation, failure, called, "announcement_document_failed")
        except CompanyRawCapacityError as exc:
            self._emit_download(CompanyDownloadIssue(
                operation, "company_raw_capacity_unavailable", material_accepted=False, action="stop", capacity=exc.snapshot,
            ))
            return CompanySyncStepResult(False, False, gaps=("company_raw_capacity_unavailable",), pending_count=None)
        self._emit_download(CompanyDocumentDownloadProgress(
            code=target.code, source=target.source, reused=not called, recovered=isinstance(work, _RequiredDocument), official_id=target.official_id,
        ))
        return CompanySyncStepResult(False, True, accepted_inputs=1, source_calls=int(called))

    def _document_failure(
        self, item: Disclosure, operation: CompanyDownloadOperation, exc: CompanyDocumentFailure | CompanySourceFailure,
        called: bool, gap: str,
    ) -> CompanySyncStepResult:
        assert self._configuration is not None and self._policies is not None and self._wall_clock is not None
        code = CompanyFailureCode(exc)
        transient = IsTransientCompanyDocumentFailure(code) and code != "access_denied"
        delay = self._configuration.document_transient_retry_seconds if transient else self._configuration.document_failure_retry_seconds
        attempted_at = self._wall_clock()
        if attempted_at.utcoffset() is None:
            raise ValueError("company_document_retry_clock_invalid") from exc
        retry_at = attempted_at + timedelta(seconds=delay)
        self._material.document_failure(item, self._policies.standard, code, attempted_at, retry_at,
            payload_hash=exc.payload_hash if isinstance(exc, CompanyDocumentFailure) else None,
            diagnostic=exc.diagnostic if isinstance(exc, CompanyDocumentFailure) else None)
        self._emit_download(CompanyDownloadIssue(operation, code, material_accepted=False, action="defer", retry_at=retry_at))
        return CompanySyncStepResult(False, True, source_calls=int(called), gaps=(gap,), failure=code)

    def _download_document(
        self, item: Disclosure, target: CompanyDocumentDownloadTarget, *, recovered: bool,
    ) -> None:
        self._source.download_document(item, attempt_limit=1)
        self._emit_download(CompanyDocumentDownloadProgress(
            code=target.code,
            source=target.source,
            reused=False,
            recovered=recovered,
            official_id=target.official_id,
            state="downloaded",
        ))


__all__ = ["CompanyRatioProducer", "CompanyAnnouncementProducer"]


def _supplemental_unlock(title: str) -> bool:
    return IsCompanyUnlockListingNotice(title) and not CompanyDocumentNeedsReview(title)


def _risk_priority(title: str) -> bool:
    # A voluntary pledge not to reduce is still reviewable. Other risk cues in
    # the same title, and formal risk dependencies, keep their normal priority.
    return CompanyDocumentRiskRelevant(title.replace("自愿承诺不减持", ""))


def _required_identity(document: _RequiredDocument | None) -> str:
    return CanonicalDigest(None if document is None else (document.disclosure, document.body_hash.hex(), document.payload_hash.hex()))
