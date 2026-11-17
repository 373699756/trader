"""Bounded current-source producers; unavailable coverage never clears old facts."""

import hashlib
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, timedelta

from trader.download.application.company_source_ports import (
    CompanyAnnualIncomeSource, CompanyNewsSource, CompanyOfficialDocumentSource, CompanyRatioSource,
)
from trader.download.domain.company_annual_income import CompanyAnnualIncomeRequest
from trader.download.domain.company_events import ContractMeasurements
from trader.download.domain.company_evidence import (
    CompanyInputValidationFailure, CompanyDocumentFailure, CompanyDocumentFailureRules, CompanyDocumentPolicies,
    CompanySyncConfiguration, Disclosure, DisclosurePartition, EvidenceMarket, StructuredCompanyQuery,
)
from datetime import datetime
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_sync import (
    CompanySyncFamily, CompanySyncPhasePlan, CompanySyncPhaseState, CompanySyncRoundScope, CompanySyncStepResult,
)
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.download.infra.company_field_recovery import ReadCompanyFieldRecovery, ReadRequiredCompanyMaterialDependencies
from trader.download.infra.company_material_index import SQLiteCompanyMaterialIndex
from trader.download.infra.company_raw_files import CompanyRawCapacityError
from trader.download.infra.company_sync_inputs import CompanyRecoveredInput, SQLiteCompanySyncInputStore
from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from pathlib import Path
from trader.infra.sqlite_policy import SqliteIoPolicy


class CompanyRatioProducer:
    def __init__(self, inputs: SQLiteCompanySyncInputStore, source: CompanyRatioSource, query: StructuredCompanyQuery, *, current_codes: tuple[str, ...] | None = None) -> None:
        if query.component not in {"pledge", "unlock"}:
            raise ValueError("company_ratio_producer_component_invalid")
        self._inputs, self._source, self._query = inputs, source, query
        self._current_codes = current_codes
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
            day = self._source.pledge_day(scope.cutoff)
            self._inputs.freeze_pledge_day(scope.identity, day, scope.cutoff)
            return CompanySyncStepResult(False, True, accepted_inputs=1, source_calls=1)
        query = replace(self._query, start=day, end=day) if day is not None else self._query
        cursor = self._inputs.ratio_cursor(scope.identity, query, scope.cutoff)
        if cursor is not None:
            page = self._source.fetch(cursor, observed_at=scope.cutoff)
            self._inputs.accept_ratios(scope.identity, cursor, page, scope.cutoff, eligible_codes=self._current_codes)
            return CompanySyncStepResult(False, True, accepted_inputs=len(page.ratios), source_calls=1)
        # These decoded values lack a proven total-share denominator and
        # absence/coverage contract. Retain them as observations, not facts.
        return CompanySyncStepResult(True, False, pending_count=0)


class CompanyAnnualIncomeProducer:
    def __init__(self, inputs: SQLiteCompanySyncInputStore, source: CompanyAnnualIncomeSource, facts: tuple[CompanyFacts, ...], *, current_codes: tuple[str, ...] | None = None) -> None:
        self._inputs, self._source = inputs, source
        self._current_codes = current_codes
        requests: list[CompanyAnnualIncomeRequest] = []
        for fact in facts:
            years = tuple(sorted({year for event in fact.positive_events if isinstance(event.measurements, ContractMeasurements)
                and event.measurements.reference_annual_revenue_cny is None
                for year in range(event.first_disclosed_at.year - 2, event.first_disclosed_at.year)}))
            if years:
                requests.append(CompanyAnnualIncomeRequest(fact.code, tuple(date(year, 12, 31) for year in years[-5:])))
        self._requests = tuple(requests)
        self._plan = CompanySyncPhasePlan(CompanySyncFamily.ANNUAL_INCOME, CanonicalDigest(self._requests),
            ("annual_income_first_disclosure_revision_unverified",) if requests else ())

    @property
    def plan(self) -> CompanySyncPhasePlan:
        return self._plan

    def _next(self, scope: CompanySyncRoundScope) -> CompanyAnnualIncomeRequest | None:
        return next((request for request in self._requests if not self._inputs.task_done(scope.identity, "annual_income", CanonicalDigest(request))), None)

    def state(self, scope: CompanySyncRoundScope) -> CompanySyncPhaseState:
        request = self._next(scope)
        return CompanySyncPhaseState(CanonicalDigest(request), request is not None and (self._current_codes is None or request.code in self._current_codes))

    def advance(self, scope: CompanySyncRoundScope, *, cancel_requested: Callable[[], bool], deadline: float) -> CompanySyncStepResult:
        request = self._next(scope)
        if request is None:
            return CompanySyncStepResult(True, False, pending_count=0)
        if self._current_codes is not None and request.code not in self._current_codes:
            self._inputs.complete_task(scope.identity, "annual_income", CanonicalDigest(request), 0, CanonicalDigest(("ineligible", request)))
            return CompanySyncStepResult(False, True, gaps=("population_members_became_ineligible",))
        rows = self._source.annual_income(request, observed_at=scope.cutoff)
        if any(item.code != request.code or item.report_date not in request.report_dates for item in rows):
            raise CompanyInputValidationFailure("company_annual_income_response_scope_mismatch")
        self._inputs.accept_annual(scope.identity, CanonicalDigest(request), rows, scope.cutoff)
        return CompanySyncStepResult(False, True, len(rows), 1)


class CompanyNewsProducer:
    def __init__(self, inputs: SQLiteCompanySyncInputStore, material: SQLiteCompanyMaterialIndex, source: CompanyNewsSource, *, fill: bool, current_codes: tuple[str, ...] | None = None) -> None:
        self._inputs, self._material, self._source, self._fill = inputs, material, source, fill
        self._current_codes = current_codes
        self._plan = CompanySyncPhasePlan(CompanySyncFamily.NEWS, CanonicalDigest(("news", fill)),
            ("news_skipped_by_fill",) if fill else ("news_metadata_does_not_verify_issuer_risk",))

    @property
    def plan(self) -> CompanySyncPhasePlan:
        return self._plan

    def _next(self, scope: CompanySyncRoundScope) -> str | None:
        return None if self._fill else next((code for code in scope.codes if not self._inputs.task_done(scope.identity, "news", code)), None)

    def state(self, scope: CompanySyncRoundScope) -> CompanySyncPhaseState:
        code = self._next(scope)
        return CompanySyncPhaseState(CanonicalDigest(code), code is not None and (self._current_codes is None or code in self._current_codes))

    def advance(self, scope: CompanySyncRoundScope, *, cancel_requested: Callable[[], bool], deadline: float) -> CompanySyncStepResult:
        code = self._next(scope)
        if code is None:
            return CompanySyncStepResult(True, False, pending_count=len(scope.codes) if self._fill else 0)
        if self._current_codes is not None and code not in self._current_codes:
            self._inputs.complete_task(scope.identity, "news", code, 0, CanonicalDigest(("ineligible", code)))
            return CompanySyncStepResult(False, True, gaps=("population_members_became_ineligible",))
        rows = self._source.news(code, observed_at=scope.cutoff)
        if (len(rows) > 500 or any(row.kind != "news" or row.codes != (code,) or row.published_at > scope.cutoff
            or row.published_at.date() < scope.cutoff.date() - timedelta(days=365) for row in rows)):
            raise CompanyInputValidationFailure("company_news_response_scope_mismatch")
        if rows:
            self._material.register_disclosures(rows)
        self._inputs.complete_task(scope.identity, "news", code, len(rows), CanonicalDigest(rows))
        return CompanySyncStepResult(False, True, len(rows), 1)


@dataclass(frozen=True, slots=True)
class _RequiredDocument:
    disclosure: Disclosure
    body_hash: bytes
    payload_hash: bytes


class CompanyAnnouncementProducer:
    def __init__(self, inputs: SQLiteCompanySyncInputStore, material: SQLiteCompanyMaterialIndex,
        source: CompanyOfficialDocumentSource, scope: CompanySyncRoundScope, *, sqlite_policy: SqliteIoPolicy, extraction_hash: bytes,
        io_budget: CompanySyncIoBudget | None = None, current_codes: tuple[str, ...] | None = None,
        configuration: CompanySyncConfiguration | None = None, policies: CompanyDocumentPolicies | None = None,
        wall_clock: Callable[[], datetime] | None = None) -> None:
        self._inputs, self._material, self._source, self._sqlite_policy = inputs, material, source, sqlite_policy
        self._extraction_hash = extraction_hash
        self._current_codes = scope.codes if current_codes is None else current_codes
        self._io_budget = io_budget
        self._configuration, self._policies, self._wall_clock = configuration, policies, wall_clock
        markets = tuple(sorted({EvidenceMarket(code) for code in scope.codes}))
        self._partitions = tuple(DisclosurePartition(market, start, end)
            for start, end in ((scope.cutoff.date(), scope.cutoff.date()),
                (scope.cutoff.date() - timedelta(days=365), scope.cutoff.date() - timedelta(days=1)))
            for market in markets)
        self._plan = CompanySyncPhasePlan(CompanySyncFamily.ANNOUNCEMENTS, CanonicalDigest(self._partitions), (
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

    def _initialized(self, scope: CompanySyncRoundScope) -> bool:
        return self._inputs.task_done(scope.identity, "announcements", "initialize")

    def _partition(self, scope: CompanySyncRoundScope) -> DisclosurePartition | None:
        return next((pending for partition in self._partitions for pending in self._material.pending_partitions(partition)
            if not self._inputs.task_done(scope.identity, "announcements", "ineligible_partition:" + CanonicalDigest(pending))), None)

    def _document(self, scope: CompanySyncRoundScope) -> _RequiredDocument | None:
        if self._configuration is None or self._policies is None or self._wall_clock is None:
            return None
        with self._session(self._material.path) as connection, SqliteTransaction(connection):
            dependencies = ReadRequiredCompanyMaterialDependencies(connection, self._extraction_hash)
            with self._session(self._material.facts_path) as formal, SqliteTransaction(formal):
                recoveries = ReadCompanyFieldRecovery(formal, connection, scope.codes, scope.cutoff, self._material.path.parent, field_rule_hash=CompanyFieldProofRulesHash())
                valid = frozenset((item.code, item.source, item.evidence_id, item.record_hash, item.body_hash) for item in dependencies
                    if item.code in scope.codes and formal.run("SELECT 1 FROM company_input_receipt WHERE code=? AND source=? AND source_key=? AND record_hash=? AND body_hash=? AND extraction_hash=? AND outcome='valid'",
                    (item.code, item.source, item.evidence_id, item.record_hash, item.body_hash, self._extraction_hash)).fetchone() is not None)
            permitted = tuple((item.gap.code, item.gap.evidence_id) for item in recoveries if item.action in {"download", "extract"})
            if len(dependencies) > 120000:
                raise ValueError("company_required_material_scope_limit")
            for dependency in dependencies:
                if ((dependency.code, dependency.evidence_id) not in permitted
                    or (dependency.code, dependency.source, dependency.evidence_id, dependency.record_hash, dependency.body_hash) not in valid
                    or dependency.code not in scope.codes or dependency.record_hash != dependency.current_record_hash
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
                retry = self._material.document_retry(item, self._policies.standard)
                if retry is not None and retry.retry_after > self._wall_clock():
                    continue
                required = _RequiredDocument(item, dependency.body_hash, row["payload_hash"])
                if (not self._inputs.task_done(scope.identity, "announcements", "document:" + _required_identity(required))
                    and not self._inputs.task_done(scope.identity, "announcements", "ineligible_document:" + _required_identity(required))):
                    return required
        return None

    def state(self, scope: CompanySyncRoundScope) -> CompanySyncPhaseState:
        if not self._initialized(scope):
            return CompanySyncPhaseState(CanonicalDigest("initialize"), False)
        partition = self._partition(scope)
        if partition is not None:
            return CompanySyncPhaseState(CanonicalDigest(partition), any(EvidenceMarket(code) == partition.market for code in self._current_codes))
        document = self._document(scope)
        payload_hash = self._source.document_payload_hash(document.disclosure) if document is not None else None
        return CompanySyncPhaseState(_required_identity(document), document is not None and document.disclosure.codes[0] in self._current_codes and payload_hash is None,
            document is not None and payload_hash == document.payload_hash)

    def advance(self, scope: CompanySyncRoundScope, *, cancel_requested: Callable[[], bool], deadline: float) -> CompanySyncStepResult:
        if not self._initialized(scope):
            for initial_partition in self._partitions:
                self._material.register_partition(initial_partition, scope.cutoff)
                if initial_partition.start == initial_partition.end == scope.cutoff.date():
                    self._material.restart_partition(initial_partition)
            self._inputs.complete_task(scope.identity, "announcements", "initialize", 0, CanonicalDigest(self._partitions))
            return CompanySyncStepResult(False, True)
        partition = self._partition(scope)
        if partition is not None:
            if not any(EvidenceMarket(code) == partition.market for code in self._current_codes):
                self._inputs.complete_task(scope.identity, "announcements", "ineligible_partition:" + CanonicalDigest(partition), 0, CanonicalDigest(("ineligible", partition)))
                return CompanySyncStepResult(False, True, gaps=("population_members_became_ineligible",))
            page = self._source.fetch(partition, order="desc")
            if page.total > 100 * partition.page_size and partition.start < partition.end:
                self._material.split_partition(partition, scope.cutoff)
                return CompanySyncStepResult(False, True, source_calls=1)
            else:
                self._material.commit_page(partition, page, scope.cutoff)
            return CompanySyncStepResult(False, True, len(page.rows), 1)
        document = self._document(scope)
        if document is None:
            return CompanySyncStepResult(True, False, pending_count=None, gaps=("announcement_required_material_not_fully_verified",))
        if document.disclosure.codes[0] not in self._current_codes:
            self._inputs.complete_task(scope.identity, "announcements", "ineligible_document:" + _required_identity(document), 0, _required_identity(document))
            return CompanySyncStepResult(False, True, gaps=("population_members_became_ineligible",))
        called = not self._source.document_ready(document.disclosure)
        try:
            if called:
                self._source.download_document(document.disclosure, attempt_limit=1)
            extraction = self._source.extract_document(document.disclosure)
        except CompanyDocumentFailure as exc:
            assert self._configuration is not None and self._policies is not None and self._wall_clock is not None
            transient = next((transient for failure, _route, transient in CompanyDocumentFailureRules() if failure == exc.reason), False)
            delay = self._configuration.document_transient_retry_seconds if transient else self._configuration.document_failure_retry_seconds
            attempted_at = self._wall_clock()
            if attempted_at.utcoffset() is None:
                raise ValueError("company_document_retry_clock_invalid") from exc
            self._material.document_failure(document.disclosure, self._policies.standard, exc.reason, attempted_at,
                attempted_at + timedelta(seconds=delay), payload_hash=exc.payload_hash, diagnostic=exc.diagnostic)
            return CompanySyncStepResult(False, False, failure=exc.reason, gaps=("required_original_recovery_failed",))
        except CompanyRawCapacityError:
            return CompanySyncStepResult(False, False, gaps=("company_raw_capacity_unavailable",), pending_count=None)
        if extraction.payload_hash != document.payload_hash or hashlib.sha256(extraction.text.encode()).digest() != document.body_hash:
            self._inputs.complete_task(scope.identity, "announcements", "document:" + _required_identity(document), 0, CanonicalDigest(extraction.payload_hash.hex()))
            return CompanySyncStepResult(False, False, gaps=("required_original_hash_mismatch",), failure="required_original_hash_mismatch")
        try:
            accepted = self._material.recover_required_text(document.disclosure.identity, extraction.text, scope.cutoff)
        except CompanyRawCapacityError:
            return CompanySyncStepResult(False, False, gaps=("company_raw_capacity_unavailable",), pending_count=None)
        if not accepted:
            return CompanySyncStepResult(False, False, gaps=("required_original_not_accepted",), failure="required_original_not_accepted")
        self._inputs.record_recovered(scope.identity, CompanyRecoveredInput(document.disclosure.codes[0], document.disclosure.source,
            document.disclosure.identity, document.disclosure.digest, document.body_hash))
        self._inputs.complete_task(scope.identity, "announcements", "document:" + _required_identity(document), 1,
            CanonicalDigest((_required_identity(document), extraction.payload_hash.hex())))
        return CompanySyncStepResult(False, True, 1, int(called))


__all__ = ["CompanyRatioProducer", "CompanyAnnualIncomeProducer", "CompanyNewsProducer", "CompanyAnnouncementProducer"]


def _required_identity(document: _RequiredDocument | None) -> str:
    return CanonicalDigest(None if document is None else (document.disclosure, document.body_hash.hex(), document.payload_hash.hex()))
