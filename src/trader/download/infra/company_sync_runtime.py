"""Company resource orchestration with explicit clients and one formal owner."""

import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from trader.download.application.company_financial_producer import BuildCompanyFinancialRoundQuery, CompanyFinancialProducer
from trader.download.application.company_source_ports import (
    CompanyAnnualIncomeSource, CompanyFinancialObservationSource, CompanyNewsSource, CompanyOfficialDocumentSource, CompanyRatioSource,
)
from trader.download.application.company_sync import CompanySyncProducer, ResumeCompanySyncPendingStep, RunCompanySyncRound
from trader.download.domain.company_events import CompanyDataState, CompanyEventPolicy
from trader.download.domain.company_evidence import (
    CompanyDatabaseIncompatibleError, CompanyDownloadStore, CompanyDocumentPolicies, CompanySyncConfiguration, EvidenceMarket, StructuredCompanyQuery,
)
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_document_links import CompanyPublishedEvidenceLink
from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.domain.company_sync import (
    CompanySyncBudget, CompanySyncFamily, CompanySyncFamilyResult, CompanySyncInterrupted, CompanySyncPhasePlan, CompanySyncPhaseState,
    CompanySyncRoundResult, CompanySyncRoundScope, CompanySyncStepResult, CompanySourceCancelled,
)
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_document_link_rows import ReadCompanyDocumentLinks
from trader.download.infra.company_facts_index import CompanyPublicationIdentity, ReadCompanyPublicationIdentity, SQLiteCompanyFactsIndex
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_field_recovery import RegisterCompanyMaterialDependencies
from trader.download.infra.company_material_index import SQLiteCompanyMaterialIndex
from trader.download.infra.company_normalized_index import CompanyNormalizationBaseline, SQLiteCompanyNormalizedIndex
from trader.download.infra.company_sync_inputs import SQLiteCompanySyncInputStore
from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.download.infra.company_sync_preparation import PrepareCompanyIssuerInputs
from trader.download.infra.company_sync_producers import (
    CompanyAnnouncementProducer, CompanyNewsProducer, CompanyRatioProducer,
)
from trader.download.infra.company_sync_annual_income import CompanyAnnualIncomeProducer
from trader.download.infra.company_facts_protocol import POSITIVE_TYPES
from trader.download.domain.company_events import CompanyPositiveType
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import SqlitePublicationError


class CompanySyncRuntime:
    """Injected resources only; construction opens no files, HTTP or executors."""

    def __init__(
        self, normalized: SQLiteCompanyNormalizedIndex, facts: SQLiteCompanyFactsIndex,
        material: SQLiteCompanyMaterialIndex, journal: SQLiteCompanySyncRoundJournal, inputs: SQLiteCompanySyncInputStore,
        *, event_policy: CompanyEventPolicy, sqlite_policy: SqliteIoPolicy, read_document: Callable[[bytes], str],
        financial_source: CompanyFinancialObservationSource, ratio_source: CompanyRatioSource,
        annual_source: CompanyAnnualIncomeSource, news_source: CompanyNewsSource, official_source: CompanyOfficialDocumentSource,
        io_budget: CompanySyncIoBudget | None = None,
        document_configuration: CompanySyncConfiguration | None = None, document_policies: CompanyDocumentPolicies | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        observe: Callable[[CompanySyncFamilyResult], None] | None = None,
        activity: Callable[[CompanySyncFamily], AbstractContextManager[None]] | None = None,
        source_ready: Callable[[], bool] | None = None,
    ) -> None:
        paths = (normalized.path, facts.path, material.path, journal.path, inputs.path)
        if any(path.is_symlink() for path in paths) or any(
            left.resolve() == right.resolve() or left.exists() and right.exists() and left.samefile(right)
            for position, left in enumerate(paths) for right in paths[position + 1:]
        ):
            raise ValueError("company_sync_store_paths_must_differ")
        self.normalized, self.facts, self.material, self.journal, self.inputs = normalized, facts, material, journal, inputs
        self._event_policy, self._sqlite_policy, self._read_document = event_policy, sqlite_policy, read_document
        self._financial_source, self._ratio_source, self._annual_source = financial_source, ratio_source, annual_source
        self._news_source, self._official_source = news_source, official_source
        self._io_budget = io_budget
        self._document_configuration, self._document_policies = document_configuration, document_policies
        self._wall_clock = wall_clock
        self._observe = observe
        self._activity = activity
        self._source_ready = source_ready
        self._execution_scope: CompanySyncRoundScope | None = None
        self._execution_round_id = ""

    def initialize(self, codes: tuple[str, ...], cutoff: datetime, *, baseline: CompanyNormalizationBaseline | None = None) -> None:
        """Validate every existing store before creating any missing store.

        Current complete schemas are mandatory. A legacy source codec is only
        used by the separately explicit research driver, never this operation.
        An empty target receives real unavailable facts, without source proof.
        """
        scope = CompanySyncRoundScope(codes, cutoff)
        validations: tuple[tuple[CompanyDownloadStore, Callable[[], None]], ...] = (
            (CompanyDownloadStore.COLLECTION, self.material.validate_existing),
            (CompanyDownloadStore.FACTS, self.facts.validate_existing),
            (CompanyDownloadStore.NORMALIZED, self.normalized.validate_existing),
            (CompanyDownloadStore.INPUTS, self.inputs.validate_existing),
            (CompanyDownloadStore.REQUEST_JOURNAL, self.journal.validate_existing),
        )
        for store, validate in validations:
            try:
                validate()
            except sqlite3.DatabaseError as exc:
                if str(exc) != "sqlite_schema_unsupported":
                    raise
                raise CompanyDatabaseIncompatibleError(store) from exc
        try:
            self.facts.verify_publication()
        except SqlitePublicationError:
            if baseline is not None:
                self.normalized.seed(baseline)
            elif not self.normalized.recover_publication_seal():
                raise
            self.facts.verify_publication()
        self.normalized.initialize()
        self.material.initialize()
        self.inputs.initialize()
        self.journal.initialize()
        if baseline is not None:
            self.normalized.seed(baseline)
            return
        with self._session(self.facts.path, check=self._io_budget.check if self._io_budget is not None else lambda: None, read_only=True) as formal:
            publication = ReadCompanyPublicationIdentity(formal)
        with self._session(self.normalized.path, check=self._io_budget.check if self._io_budget is not None else lambda: None, read_only=True) as normalized:
            normalized_publication = ReadCompanyPublicationIdentity(normalized)
        if publication is None and normalized_publication is None:
            initial = tuple(_unavailable(code, cutoff) for code in codes)
            identity = CompanyPublicationIdentity(bytes.fromhex(CanonicalDigest(("initial_unavailable_population", scope))), self.facts.extraction_hash, cutoff)
            self.normalized.seed(CompanyNormalizationBaseline(initial, (), (), (), identity, CompanyFieldProofRulesHash()))
        elif publication is None or normalized_publication is None:
            raise ValueError("company_sync_explicit_typed_baseline_required")

    def run(
        self, codes: tuple[str, ...], cutoff: datetime, budget: CompanySyncBudget, *,
        cancel_requested: Callable[[], bool], monotonic: Callable[[], float],
        financial_query: StructuredCompanyQuery | None = None, fill: bool = False, new_refresh: bool = True,
        news_codes: tuple[str, ...] = (),
    ) -> CompanySyncRoundResult:
        self._execution_scope, self._execution_round_id = None, ""
        try:
            return self._run(codes, cutoff, budget, cancel_requested=cancel_requested, monotonic=monotonic,
                financial_query=financial_query, fill=fill, new_refresh=new_refresh, news_codes=news_codes)
        except (CompanySyncInterrupted, CompanySourceCancelled) as exc:
            if isinstance(exc, CompanySourceCancelled) and not cancel_requested() and monotonic() < budget.deadline:
                raise
            scope = self._execution_scope if self._execution_scope is not None else CompanySyncRoundScope(codes, cutoff)
            return CompanySyncRoundResult(self._execution_round_id, scope, (), cancel_requested(), not cancel_requested(), "")

    def _run(
        self, codes: tuple[str, ...], cutoff: datetime, budget: CompanySyncBudget, *,
        cancel_requested: Callable[[], bool], monotonic: Callable[[], float],
        financial_query: StructuredCompanyQuery | None = None, fill: bool = False, new_refresh: bool = True,
        news_codes: tuple[str, ...] = (),
    ) -> CompanySyncRoundResult:
        if cancel_requested() or monotonic() >= budget.deadline:
            return CompanySyncRoundResult("", CompanySyncRoundScope(codes, cutoff), (), cancel_requested(), not cancel_requested(), "")
        active = self.journal.active()
        exhausted_same_day = active is not None and cutoff.astimezone(active.scope.cutoff.tzinfo).date() == active.scope.cutoff.date() and (
            self.journal.source_calls(active.round_id) >= active.max_source_calls
        )
        if active is not None and new_refresh and (codes != active.scope.codes or cutoff != active.scope.cutoff) and self.journal.can_refresh(active.round_id):
            if cutoff < active.scope.cutoff:
                raise ValueError("company_sync_refresh_cutoff_before_active")
            if not exhausted_same_day:
                self.journal.finish_refresh(active.round_id)
                active = None
        if active is not None:
            scope = active.scope
            budget = replace(budget, max_source_calls=active.max_source_calls)
        else:
            scope = CompanySyncRoundScope(codes, cutoff)
        self._execution_scope = scope
        eligible_codes = tuple(code for code in scope.codes if code in codes)
        news_codes = self.inputs.freeze_news_codes(scope.identity, tuple(code for code in news_codes if code in scope.codes))
        population_gaps = tuple(reason for changed, reason in (
            (eligible_codes != scope.codes, "population_members_became_ineligible"),
            (any(code not in scope.codes for code in codes), "new_population_members_wait_for_refresh"),
        ) if changed)
        def check() -> None:
            if cancel_requested() or monotonic() >= budget.deadline:
                raise CompanySyncInterrupted("company_sync_step_interrupted")
        check()
        def annual_facts() -> tuple[CompanyFacts, ...]:
            with self._session(self.facts.path, check=check, read_only=True) as formal, SqliteTransaction(formal):
                selected = tuple(row[0] for row in formal.run(
                    "SELECT DISTINCT code FROM company_positive_event WHERE event_type=? ORDER BY code",
                    (POSITIVE_TYPES[CompanyPositiveType.OPERATING_CONTRACT],)) if row[0] in eligible_codes)
                return ReadCompanyFactRows(formal, selected)
        query = financial_query or BuildCompanyFinancialRoundQuery(scope.cutoff)
        fields = _FieldProducer(self, monotonic, eligible_codes)
        announcements = CompanyAnnouncementProducer(self.inputs, self.material, self._official_source, scope,
            sqlite_policy=self._sqlite_policy, extraction_hash=self.facts.extraction_hash, io_budget=self._io_budget,
            current_codes=eligible_codes, configuration=self._document_configuration, policies=self._document_policies,
            wall_clock=self._wall_clock)
        producers: tuple[CompanySyncProducer, ...] = (
            CompanyFinancialProducer(self.normalized, self._financial_source, query, qualification_gaps=(
                "latest_financial_population_unverified", "financial_first_disclosure_revision_unverified"), current_codes=eligible_codes),
            CompanyRatioProducer(self.inputs, self._ratio_source, StructuredCompanyQuery("pledge", scope.cutoff.date() - timedelta(days=365), scope.cutoff.date()), current_codes=eligible_codes),
            CompanyRatioProducer(self.inputs, self._ratio_source, StructuredCompanyQuery("unlock", scope.cutoff.date(), scope.cutoff.date() + timedelta(days=min(180, self._event_policy.unlock_days))), current_codes=eligible_codes),
            announcements,
            CompanyAnnualIncomeProducer(self.inputs, self._annual_source, annual_facts, current_codes=eligible_codes),
            CompanyNewsProducer(self.inputs, self.material, self._news_source, news_codes=news_codes, fill=fill, current_codes=eligible_codes),
            fields,
        )
        plans = tuple(producer.plan for producer in producers)
        round_id = self.journal.begin(scope, plans, max_source_calls=budget.max_source_calls)
        self._execution_round_id = round_id
        self.material.set_population(eligible_codes)
        # Current local proof and exact material needs precede any optional
        # network. The same fields owner runs again after source acceptance.
        preflight_task = "preflight:" + fields.plan.request_hash
        if not self.inputs.task_done(scope.identity, "fields", preflight_task):
            recovered = ResumeCompanySyncPendingStep(self.journal, producers, scope, round_id, budget,
                monotonic=monotonic, cancel_requested=cancel_requested, observe=self._observe,
                activity=self._activity, recover_directory=announcements.recover_directory)
            if recovered:
                if budget.max_steps == 1:
                    return CompanySyncRoundResult(round_id, scope, (), False, True, "")
                budget = replace(budget, max_steps=budget.max_steps - 1)
            pending = self.journal.outstanding(round_id, CompanySyncFamily.FIELDS)
            if pending is None:
                self.journal.start(round_id, CompanySyncFamily.FIELDS, CompanySyncPhaseState(CanonicalDigest("field_preflight"), False))
            activity = self._activity(CompanySyncFamily.FIELDS) if self._activity is not None else nullcontext()
            with activity:
                try:
                    prepared = fields.advance(scope, cancel_requested=cancel_requested, deadline=budget.deadline)
                except CompanySyncInterrupted:
                    return CompanySyncRoundResult(round_id, scope, (), cancel_requested(), not cancel_requested(), "")
                self.journal.finish(round_id, CompanySyncFamily.FIELDS, replace(prepared, processed_scope_complete=False, can_continue=True, deferred=True))
                self.inputs.complete_task(scope.identity, "fields", preflight_task, prepared.accepted_inputs or 0, CanonicalDigest((prepared.changed_codes, fields.plan)))
            if self._observe is not None:
                confirmed = self.journal.family(round_id, CompanySyncFamily.FIELDS)
                if confirmed is not None:
                    self._observe(confirmed)
            if budget.max_steps == 1:
                current = self.journal.family(round_id, CompanySyncFamily.FIELDS)
                return CompanySyncRoundResult(round_id, scope, (current,) if current is not None else (), False, True, "")
            budget = replace(budget, max_steps=budget.max_steps - 1)
        try:
            result = RunCompanySyncRound(self.journal, producers, scope, budget, monotonic=monotonic,
                cancel_requested=cancel_requested, observe=self._observe, activity=self._activity,
                recover_directory=announcements.recover_directory, source_ready=self._source_ready)
        except CompanySyncInterrupted:
            return CompanySyncRoundResult(round_id, scope, (), cancel_requested(), not cancel_requested(), "")
        if population_gaps:
            result = replace(result, families=tuple(replace(item, result=replace(item.result,
                gaps=item.result.gaps + tuple(gap for gap in population_gaps if gap not in item.result.gaps))) for item in result.families))
        return result

    def clean_material(self, cutoff: datetime) -> None:
        """Explicit maintenance; publication never calls this operation."""
        self.material.clean_material(cutoff)

    def field_watermark(self, scope: CompanySyncRoundScope, eligible_codes: tuple[str, ...], check: Callable[[], None]) -> str:
        check()
        eligible = frozenset(eligible_codes)
        markets = tuple(sorted({EvidenceMarket(code) for code in eligible_codes}))
        material: list[tuple[object, ...]] = []
        with self._session(self.facts.path, check=check, read_only=True) as formal, SqliteTransaction(formal):
            facts = ReadCompanyFactRows(formal, eligible_codes)
            publication = ReadCompanyPublicationIdentity(formal)
        identities = tuple(sorted({identity for fact in facts for event in fact.risk_events + fact.positive_events for identity in event.evidence_ids}
            | {review.evidence_id for fact in facts for review in fact.review_reasons if review.evidence_id is not None}))
        with self._session(self.material.path, check=check, read_only=True) as raw, SqliteTransaction(raw):
            coverage = tuple(tuple(row) for row in raw.run(
                "SELECT market,start_date,end_date FROM coverage "
                f"WHERE market IN ({','.join('?' for _ in markets)}) ORDER BY market,start_date,end_date",
                markets,
            )) if markets else ()
            for offset in range(0, len(eligible_codes), 128):
                check()
                codes = eligible_codes[offset:offset + 128]
                for row in raw.run("""SELECT c.code,d.source,d.source_key,d.record_hash,d.body_hash,d.body_checked,d.body_retained
                    FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id
                    JOIN company_document_requirements required ON required.disclosure_id=d.id
                    WHERE d.kind='announcement' AND d.published_at>=? AND d.published_at<=?
                    AND c.code IN (""" + ",".join("?" for _ in codes) + ") ORDER BY c.code,d.source,d.source_key",
                    ((scope.cutoff.date() - timedelta(days=365)).isoformat(), scope.cutoff.isoformat(), *codes)):
                    material.append((row[0], row[1], row[2], row[3], row[4].hex() if isinstance(row[4], bytes) else None, row[5], row[6]))
            for offset in range(0, len(identities), 512):
                check()
                batch = identities[offset:offset + 512]
                for row in raw.run("SELECT c.code,d.source,d.source_key,d.record_hash,d.body_hash,d.body_checked,d.body_retained FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE d.source_key IN (" + ",".join("?" for _ in batch) + ") ORDER BY c.code,d.source,d.source_key", batch):
                    if row[0] in eligible:
                        material.append((row[0], row[1], row[2], row[3], row[4].hex() if isinstance(row[4], bytes) else None, row[5], row[6]))
        return CanonicalDigest((self.inputs.field_input_identity(scope.identity), coverage,
            tuple(sorted(set(material), key=lambda row: (str(row[0]), str(row[1]), str(row[2])))),
            publication.content_hash.hex() if publication is not None else None))

    @contextmanager
    def _session(self, path: Path, *, check: Callable[[], None], read_only: bool) -> Iterator[SqliteConnection]:
        if self._io_budget is not None:
            check()
            with self._io_budget.session(path, SqliteConnectionOptions(read_only=read_only), sqlite_policy=self._sqlite_policy) as connection:
                yield connection
            return
        def cancelled() -> bool:
            try:
                check()
            except CompanySyncInterrupted:
                return True
            return False
        check()
        try:
            with SqliteSession(path, SqliteConnectionOptions(read_only=read_only), sqlite_policy=self._sqlite_policy, cancel_requested=cancelled) as connection:
                yield connection
        except sqlite3.OperationalError as exc:
            if str(exc) == "interrupted":
                check()
            raise

    def prepare_local(self, scope: CompanySyncRoundScope, *, check: Callable[[], None], cancel_requested: Callable[[], bool], eligible_codes: tuple[str, ...]) -> CompanySyncStepResult:
        check()
        if not eligible_codes:
            return CompanySyncStepResult(True, False, pending_count=None, gaps=("population_members_became_ineligible",))
        rules_hash = CompanyFieldProofRulesHash()
        recovered = self.inputs.pending_recovered(scope.identity)
        annuals = self.inputs.annuals(scope.identity)
        with (
            self._session(self.material.path, check=check, read_only=True) as raw,
            self._session(self.facts.path, check=check, read_only=True) as formal,
            SqliteTransaction(raw), SqliteTransaction(formal),
        ):
            marker = formal.run("SELECT rules_hash FROM company_field_proof_rule WHERE singleton=1").fetchone()
            revalidate = marker is None or marker[0] != rules_hash
            all_codes = tuple(sorted(set(eligible_codes) | {row[0] for row in formal.run("SELECT code FROM company_facts ORDER BY code")})) if revalidate else eligible_codes
            old = {fact.code: fact for fact in ReadCompanyFactRows(formal, all_codes)}
            fields_by_code: dict[str, list[CompanyFieldConclusion]] = defaultdict(list)
            for field in ReadCompanyFieldResults(formal, all_codes):
                fields_by_code[field.code].append(field)
            links_by_code: dict[str, list[CompanyPublishedEvidenceLink]] = defaultdict(list)
            for link in ReadCompanyDocumentLinks(formal, all_codes):
                links_by_code[link.code].append(link)
            eligible = frozenset(eligible_codes)
            prepared = []
            for code in all_codes:
                check()
                fact = old.get(code)
                if fact is None:
                    fact = _unavailable(code, scope.cutoff)
                current_fields = fields_by_code.get(code, ())
                needs_recovery_reproof = any(field.applicable and field.value is not None and field.state != "verified"
                    and any(item.code == fact.code and item.source_key in event.evidence_ids for item in recovered
                        for event in fact.risk_events + fact.positive_events if event.event_id == field.event_id) for field in current_fields)
                prepared.append(PrepareCompanyIssuerInputs(raw, formal, fact, cutoff=scope.cutoff,
                    extraction_hash=self.facts.extraction_hash, event_policy=self._event_policy,
                    read_document=self._read_document, check=check, revalidate=revalidate or needs_recovery_reproof, correct_measurements=code in eligible,
                    include_new_announcements=True, published_links=tuple(links_by_code.get(code, ())), recovered=recovered,
                    annual_incomes=annuals))
        changed = self.normalized.publish_prepared(all_codes, tuple(item.facts for item in prepared),
            tuple(field for item in prepared for field in item.fields), tuple(receipt for item in prepared for receipt in item.receipts),
            tuple(link for item in prepared for link in item.links), scope.cutoff, field_rules_hash=rules_hash,
            sources=tuple(sorted({(receipt.source_key, receipt.source) for item in prepared for receipt in item.receipts}
                | {(link.link.evidence_id, link.source) for item in prepared for link in item.links})))
        self.inputs.acknowledge_recovered(scope.identity, tuple(item for item in recovered if item.code in all_codes))
        check()
        with (self._session(self.material.path, check=check, read_only=False) as raw,
            SqliteTransaction(raw, immediate=True), self._session(self.facts.path, check=check, read_only=True) as formal,
            SqliteTransaction(formal)):
            RegisterCompanyMaterialDependencies(raw, formal, tuple(item.facts for item in prepared), self.facts.extraction_hash)
        pending = sum(field.applicable and field.state != "verified" for item in prepared for field in item.fields)
        remaining = sum(item.pending_retained for item in prepared)
        return CompanySyncStepResult(not remaining, bool(remaining), len(prepared), changed_codes=changed, pending_count=pending + remaining,
            gaps=("field_proof_incomplete",) if pending else ())


class _FieldProducer:
    def __init__(self, runtime: CompanySyncRuntime, monotonic: Callable[[], float], eligible_codes: tuple[str, ...]) -> None:
        self._runtime, self._monotonic = runtime, monotonic
        self._eligible_codes = eligible_codes
        self._plan = CompanySyncPhasePlan(CompanySyncFamily.FIELDS, CanonicalDigest(("current_field_proof", CompanyFieldProofRulesHash().hex())), refresh_on_input_change=True)

    @property
    def plan(self) -> CompanySyncPhasePlan:
        return self._plan

    def state(self, scope: CompanySyncRoundScope) -> CompanySyncPhaseState:
        check = self._runtime._io_budget.check if self._runtime._io_budget is not None else lambda: None
        watermark = self._runtime.field_watermark(scope, self._eligible_codes, check)
        return CompanySyncPhaseState(CanonicalDigest((scope, self._plan, watermark)), False)

    def advance(self, scope: CompanySyncRoundScope, *, cancel_requested: Callable[[], bool], deadline: float) -> CompanySyncStepResult:
        def check() -> None:
            if cancel_requested() or self._monotonic() >= deadline:
                raise CompanySyncInterrupted("company_sync_local_preparation_interrupted")
        return self._runtime.prepare_local(scope, check=check, cancel_requested=cancel_requested, eligible_codes=self._eligible_codes)


def _unavailable(code: str, cutoff: datetime) -> CompanyFacts:
    return CompanyFacts(code, None, CompanyDataState.UNAVAILABLE, None, CompanyDataState.UNAVAILABLE, (), None, None,
        CompanyDataState.UNAVAILABLE, (), (), (), 0, None, None, CompanyDataState.UNAVAILABLE, cutoff, cutoff, latest_financial=None)


__all__ = ["CompanySyncRuntime"]
