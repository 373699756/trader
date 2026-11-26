"""Company resource orchestration with explicit clients and one formal owner."""

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from trader.download.application.company_financial_producer import CompanyFinancialProducer, ResolveCompanyFinancialScoringQuery
from trader.download.application.company_source_ports import (
    CompanyAnnualIncomeSource, CompanyFinancialObservationSource, CompanyOfficialDocumentSource, CompanyRatioSource,
)
from trader.download.application.company_sync import CompanySyncProducer, ResumeCompanySyncPendingStep, RunCompanySyncRound
from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview
from trader.download.domain.company_events import CompanyDataState, CompanyEventPolicy
from trader.download.domain.company_evidence import (
    CompanyDatabaseIncompatibleError, CompanyDownloadStore, CompanyDocumentPolicies, CompanyRatio, CompanySyncConfiguration, EvidenceMarket, StructuredCompanyQuery,
)
from trader.download.domain.company_facts import CompanyFacts
from trader.download.domain.company_financial_admission import CompanyFinancialAdmission, CompanyFinancialRevisionProof
from trader.download.domain.company_document_links import CompanyPublishedEvidenceLink
from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.domain.company_normalization import ApplyCompanyFinancialAdmission
from trader.download.domain.company_source_records import CompanyFinancialSourceObservation
from trader.download.domain.company_sync import (
    CompanySourceDownloadProgress, CompanySyncBudget, CompanySyncFamily, CompanySyncFamilyResult, CompanySyncPauseReason,
    CompanySyncInterrupted, CompanySyncPhasePlan, CompanySyncPhaseState,
    CompanySyncRebuildActiveRound, CompanySyncRoundIdentity, CompanySyncRoundResult, CompanySyncRoundScope, CompanySyncStepResult, CompanySourceCancelled,
)
from trader.download.domain.company_unlock_notice import IsCompanyUnlockListingNotice
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_announcement_preparation import ReadCompanyAnnouncementCoverage
from trader.download.infra.company_document_link_rows import ReadCompanyDocumentLinks
from trader.download.infra.company_facts_index import CompanyPublicationIdentity, ReadCompanyPublicationIdentity, SQLiteCompanyFactsIndex
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_field_recovery import RegisterCompanyMaterialDependencies
from trader.download.infra.company_financial_publication import CompanyFinancialAdmissionReceipts, PrepareCompanyFinancialAdmissions
from trader.download.infra.company_material_index import SQLiteCompanyMaterialIndex
from trader.download.infra.company_normalized_index import CompanyNormalizationBaseline, SQLiteCompanyNormalizedIndex
from trader.download.infra.company_sync_inputs import SQLiteCompanySyncInputStore
from trader.download.infra.company_sync_io import CompanySyncIoBudget
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.download.infra.company_sync_preparation import PrepareCompanyIssuerInputs
from trader.download.infra.company_sync_producers import (
    CompanyAnnouncementProducer, CompanyRatioProducer,
)
from trader.download.infra.company_sync_annual_income import CompanyAnnualIncomeProducer
from trader.download.infra.company_facts_protocol import POSITIVE_TYPES
from trader.download.domain.company_events import CompanyPositiveType
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import SqlitePublicationError


@dataclass(frozen=True, slots=True)
class _FieldPopulation:
    codes: tuple[str, ...]
    facts: tuple[CompanyFacts, ...]
    publication: CompanyPublicationIdentity | None


class CompanySyncRuntime:
    """Injected resources only; construction opens no files, HTTP or executors."""

    def __init__(
        self, normalized: SQLiteCompanyNormalizedIndex, facts: SQLiteCompanyFactsIndex,
        material: SQLiteCompanyMaterialIndex, journal: SQLiteCompanySyncRoundJournal, inputs: SQLiteCompanySyncInputStore,
        *, event_policy: CompanyEventPolicy, sqlite_policy: SqliteIoPolicy, read_document: Callable[[bytes], str],
        financial_source: CompanyFinancialObservationSource, ratio_source: CompanyRatioSource,
        annual_source: CompanyAnnualIncomeSource, official_source: CompanyOfficialDocumentSource,
        io_budget: CompanySyncIoBudget | None = None,
        document_configuration: CompanySyncConfiguration | None = None, document_policies: CompanyDocumentPolicies | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        observe_scope: Callable[[CompanySyncRoundScope, StructuredCompanyQuery], None] | None = None,
        observe: Callable[[CompanySyncFamilyResult], None] | None = None,
        observe_download: Callable[[CompanySourceDownloadProgress], None] | None = None,
        observe_financial_scoring: Callable[[CompanySyncRoundScope], None] | None = None,
        activity: Callable[[CompanySyncFamily], AbstractContextManager[None]] | None = None,
        source_ready: Callable[[], bool] | None = None,
        sleeper: Callable[[float], None] | None = None,
        financial_revision_proofs: tuple[CompanyFinancialRevisionProof, ...] = (),
    ) -> None:
        paths = (material.path, facts.path, journal.path)
        if any(path.is_symlink() for path in (*paths, normalized.path, inputs.path)) or any(
            left.resolve() == right.resolve() or left.exists() and right.exists() and left.samefile(right)
            for position, left in enumerate(paths) for right in paths[position + 1:]
        ) or normalized.path.resolve() != material.path.resolve() or inputs.path.resolve() != material.path.resolve():
            raise ValueError("company_sync_sources_must_share_one_separate_store")
        self.normalized, self.facts, self.material, self.journal, self.inputs = normalized, facts, material, journal, inputs
        self._event_policy, self._sqlite_policy, self._read_document = event_policy, sqlite_policy, read_document
        self._financial_source, self._ratio_source, self._annual_source = financial_source, ratio_source, annual_source
        self._official_source = official_source
        self._io_budget = io_budget
        self._document_configuration, self._document_policies = document_configuration, document_policies
        self._wall_clock = wall_clock
        self._observe_scope = observe_scope
        self._observe = observe
        self._observe_download = observe_download
        self._observe_financial_scoring = observe_financial_scoring
        self._activity = activity
        self._source_ready = source_ready
        self._sleeper = sleeper
        self._financial_revision_proofs = financial_revision_proofs
        self._execution_scope: CompanySyncRoundScope | None = None
        self._execution_round_id = ""

    def initialize(self, codes: tuple[str, ...], cutoff: datetime, *, baseline: CompanyNormalizationBaseline | None = None) -> None:
        """Validate every existing store before creating any missing store.

        A clean source store cannot reuse a request cursor for discarded inputs.
        """
        scope = CompanySyncRoundScope(codes, cutoff)
        validations: tuple[tuple[CompanyDownloadStore, Callable[[], None]], ...] = (
            (CompanyDownloadStore.FACTS, self.facts.validate_existing),
            (CompanyDownloadStore.SOURCES, self.material.validate_existing),
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
        publication = self._read_store_publication(self.facts.path)
        normalized_publication = self._read_store_publication(self.material.path)
        if baseline is None:
            if publication is None and normalized_publication is None:
                self._release_scope_for_rebuild(scope)
            elif publication is None or normalized_publication is None:
                raise ValueError("company_sync_explicit_typed_baseline_required")
        self.material.initialize()
        self.facts.initialize()
        self.journal.initialize()
        if baseline is not None:
            self.normalized.seed(baseline)
            return
        if publication is None and normalized_publication is None:
            initial = tuple(_unavailable(code, cutoff) for code in codes)
            identity = CompanyPublicationIdentity(bytes.fromhex(CanonicalDigest(("initial_unavailable_population", scope))), self.facts.extraction_hash, cutoff)
            self.normalized.seed(CompanyNormalizationBaseline(initial, (), (), (), identity, CompanyFieldProofRulesHash()))

    def _read_store_publication(self, path: Path) -> CompanyPublicationIdentity | None:
        if not path.exists():
            return None
        check = self._io_budget.check if self._io_budget is not None else lambda: None
        with self._session(path, check=check, read_only=True) as connection:
            return ReadCompanyPublicationIdentity(connection)

    def _release_scope_for_rebuild(self, scope: CompanySyncRoundScope) -> None:
        if not self.journal.path.exists():
            return
        active = self.journal.active()
        if active is None:
            return
        if scope.cutoff.date() <= active.scope.cutoff.date():
            raise CompanySyncRebuildActiveRound("company_sync_rebuild_requires_later_scope")
        if self.journal.can_refresh(active.round_id):
            self.journal.finish_refresh(active.round_id)
        elif not self.journal.defer_unconfirmed_directory(active.round_id, scope.cutoff):
            raise CompanySyncRebuildActiveRound("company_sync_rebuild_requires_previous_scope_reconciliation")

    def run(
        self, codes: tuple[str, ...], cutoff: datetime, budget: CompanySyncBudget, *,
        cancel_requested: Callable[[], bool], monotonic: Callable[[], float],
        financial_query: StructuredCompanyQuery | None = None, new_refresh: bool = True,
    ) -> CompanySyncRoundResult:
        self._execution_scope, self._execution_round_id = None, ""
        try:
            return self._run(codes, cutoff, budget, cancel_requested=cancel_requested, monotonic=monotonic,
                financial_query=financial_query, new_refresh=new_refresh)
        except (CompanySyncInterrupted, CompanySourceCancelled) as exc:
            if isinstance(exc, CompanySourceCancelled) and not cancel_requested() and monotonic() < budget.deadline:
                raise
            scope = self._execution_scope if self._execution_scope is not None else CompanySyncRoundScope(codes, cutoff)
            return self._stopped(self._execution_round_id, scope, budget, cancel_requested, monotonic)

    @staticmethod
    def _stopped(
        round_id: str, scope: CompanySyncRoundScope, budget: CompanySyncBudget,
        cancel_requested: Callable[[], bool], monotonic: Callable[[], float],
    ) -> CompanySyncRoundResult:
        cancelled = cancel_requested()
        reason: CompanySyncPauseReason | None = "deadline" if not cancelled and monotonic() >= budget.deadline else None
        return CompanySyncRoundResult(round_id, scope, (), cancelled, not cancelled, "", pause_reason=reason)

    def _run(
        self, codes: tuple[str, ...], cutoff: datetime, budget: CompanySyncBudget, *,
        cancel_requested: Callable[[], bool], monotonic: Callable[[], float],
        financial_query: StructuredCompanyQuery | None = None, new_refresh: bool = True, after_backfill: str | None = None,
    ) -> CompanySyncRoundResult:
        if cancel_requested() or monotonic() >= budget.deadline:
            return self._stopped("", CompanySyncRoundScope(codes, cutoff), budget, cancel_requested, monotonic)
        requested_scope = CompanySyncRoundScope(codes, cutoff)
        requested_source_limit = budget.max_source_calls
        active = self.journal.active() if after_backfill is None else None
        current_codes = frozenset(codes)
        exhausted = active is not None and active.max_source_calls is not None and active.source_calls >= active.max_source_calls
        resume_exhausted = active is not None and exhausted and (
            budget.max_source_calls is None or cutoff.astimezone(active.scope.cutoff.tzinfo).date() == active.scope.cutoff.date())
        if active is not None and new_refresh and (codes != active.scope.codes or cutoff != active.scope.cutoff) and self.journal.can_refresh(active.round_id):
            if cutoff < active.scope.cutoff:
                raise ValueError("company_sync_refresh_cutoff_before_active")
            if not resume_exhausted:
                # A terminal predecessor can contribute its closed batch before
                # its round is released for a new network scope.
                self._publish_financial_scoring(
                    active.scope,
                    ResolveCompanyFinancialScoringQuery(active.scope.cutoff, next(plan for plan in active.plans if plan.family is CompanySyncFamily.FINANCIAL)),
                    tuple(code for code in active.scope.codes if code in current_codes),
                )
                self.journal.finish_refresh(active.round_id)
                active = None
        elif (active is not None and new_refresh and requested_scope.cutoff.date() > active.scope.cutoff.date()
            and self.journal.defer_unconfirmed_directory(active.round_id, requested_scope.cutoff)):
            logging.getLogger(__name__).warning("公司旧轮次公告响应未确认，保留已占用额度和原请求。新范围独立推进。")
            active = None
        if active is not None:
            scope = active.scope
            self._execution_round_id = active.round_id
            if budget.max_source_calls is not None and active.max_source_calls is not None:
                budget = replace(budget, max_source_calls=active.max_source_calls)
        else:
            scope = requested_scope
        self._execution_scope = scope
        eligible_codes = tuple(code for code in scope.codes if code in current_codes)
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
        plan = next(plan for plan in active.plans if plan.family is CompanySyncFamily.FINANCIAL) if active else None
        query = financial_query or ResolveCompanyFinancialScoringQuery(scope.cutoff, plan)
        def publish_financial_scoring(selected: CompanySyncRoundScope) -> None:
            check()
            self._publish_financial_scoring(selected, query, eligible_codes)

        fields = _FieldProducer(self, monotonic, eligible_codes, query)
        announcements = CompanyAnnouncementProducer(self.inputs, self.material, self._official_source, scope,
            sqlite_policy=self._sqlite_policy, extraction_hash=self.facts.extraction_hash, io_budget=self._io_budget,
            current_codes=eligible_codes, configuration=self._document_configuration, policies=self._document_policies,
            wall_clock=self._wall_clock, observe_download=self._observe_download)
        annual = CompanyAnnualIncomeProducer(self.inputs, self._annual_source, annual_facts,
            current_codes=eligible_codes, observe_download=self._observe_download)
        producers: tuple[CompanySyncProducer, ...] = (
            CompanyFinancialProducer(self.normalized, self._financial_source, query, qualification_gaps=(
                "latest_financial_population_unverified", "financial_first_disclosure_revision_unverified"),
                current_codes=eligible_codes, observe_download=self._observe_download, publish_scoring=publish_financial_scoring),
            CompanyRatioProducer(self.inputs, self._ratio_source,
                StructuredCompanyQuery("pledge", scope.cutoff.date() - timedelta(days=365), scope.cutoff.date()),
                current_codes=eligible_codes, observe_download=self._observe_download),
            CompanyRatioProducer(self.inputs, self._ratio_source,
                StructuredCompanyQuery("unlock", scope.cutoff.date(), scope.cutoff.date() + timedelta(days=min(180, self._event_policy.unlock_days))),
                current_codes=eligible_codes, observe_download=self._observe_download),
            announcements,
            annual,
            fields,
        )
        plans = tuple(producer.plan for producer in producers)
        if (new_refresh and active is not None and requested_scope.cutoff.date() > scope.cutoff.date()
            and CompanySyncRoundIdentity(scope, plans) == active.round_id and self.journal.can_handoff_backfill(active.round_id, requested_scope.cutoff)
            and self.normalized.cursor(query, scope.codes, scope.cutoff) is None
            and self._ratio_inputs_closed(scope)
            and self.material.can_retain_backfill(tuple(sorted({EvidenceMarket(code) for code in scope.codes})), scope.cutoff.date(), requested_scope.cutoff.date())
            and self.inputs.pending_annual_request(scope.identity) is None and not annual.state(scope).source_required):
            # Source plans are frozen. The journal retains both local proof-rule
            # identities when the same scope needs a new proof before handoff.
            self.journal.begin(scope, plans, max_source_calls=budget.max_source_calls)
            finished = ResumeCompanySyncPendingStep(self.journal, (fields,), scope, active.round_id, budget,
                monotonic=monotonic, cancel_requested=cancel_requested)
            if finished and budget.max_steps is not None:
                if budget.max_steps == 1:
                    return CompanySyncRoundResult(active.round_id, scope, (), False, True, "", pause_reason="step_limit")
                budget = replace(budget, max_steps=budget.max_steps - 1)
            check()
            publish_financial_scoring(scope)
            scoring_may_be_deferred = self.normalized.has_pending_publication()
            state = fields.state(scope)
            previous = self.journal.family(active.round_id, CompanySyncFamily.FIELDS)
            if self.journal.completed_state(active.round_id, CompanySyncFamily.FIELDS) != state or previous is None or previous.result.can_continue:
                self.journal.start(active.round_id, CompanySyncFamily.FIELDS, state)
                prepared = fields.advance(scope, cancel_requested=cancel_requested, deadline=budget.deadline)
                self.journal.finish(active.round_id, CompanySyncFamily.FIELDS, prepared)
                if budget.max_steps is not None:
                    if budget.max_steps == 1:
                        return CompanySyncRoundResult(active.round_id, scope, (), False, True, "", pause_reason="step_limit")
                    budget = replace(budget, max_steps=budget.max_steps - 1)
            last_field = self.journal.family(active.round_id, CompanySyncFamily.FIELDS)
            if (last_field is not None and last_field.result.processed_scope_complete and not last_field.result.failure
                and not self.normalized.has_pending_publication() and not self.inputs.pending_recovered(scope.identity)
                and self._annual_inputs_settled(scope, check) and not annual.state(scope).source_required):
                if scoring_may_be_deferred:
                    publish_financial_scoring(scope)
                check()
                return self._run(codes, cutoff, replace(budget, max_source_calls=requested_source_limit), cancel_requested=cancel_requested, monotonic=monotonic,
                    financial_query=financial_query, new_refresh=new_refresh, after_backfill=active.round_id)
        round_id = (self.journal.begin(scope, plans, max_source_calls=budget.max_source_calls) if after_backfill is None else
            self.journal.begin(scope, plans, max_source_calls=budget.max_source_calls, after_backfill=after_backfill))
        if after_backfill is not None:
            logging.getLogger(__name__).warning("公司跨日交接：旧轮次 %s 公告回补未完成，已确认目录页与请求账本保留。新轮次 %s 截止 %s，优先获取新日资料，并复核上次盘中所查日期。", after_backfill[:12], round_id[:12], scope.cutoff.isoformat())
        self._execution_round_id = round_id
        if self._observe_scope is not None:
            self._observe_scope(scope, query)
        self.material.set_population(eligible_codes)
        if not self.inputs.task_done(scope.identity, "announcements", "initialize"):
            self.material.recheck_prior_intraday(tuple(sorted({EvidenceMarket(code) for code in scope.codes})), scope.cutoff.date())
        # Current local proof and exact material needs precede any optional
        # network. The same fields owner runs again after source acceptance.
        preflight_task = "preflight:" + fields.plan.request_hash
        if not self.inputs.task_done(scope.identity, "fields", preflight_task):
            recovered = ResumeCompanySyncPendingStep(self.journal, producers, scope, round_id, budget,
                monotonic=monotonic, cancel_requested=cancel_requested, observe=self._observe,
                activity=self._activity, recover_directory=announcements.recover_directory)
            if recovered:
                if budget.max_steps == 1:
                    return CompanySyncRoundResult(round_id, scope, (), False, True, "", pause_reason="step_limit")
                if budget.max_steps is not None:
                    budget = replace(budget, max_steps=budget.max_steps - 1)
            pending = self.journal.outstanding(round_id, CompanySyncFamily.FIELDS)
            if pending is None:
                self.journal.start(round_id, CompanySyncFamily.FIELDS, CompanySyncPhaseState(CanonicalDigest("field_preflight"), False))
            activity = self._activity(CompanySyncFamily.FIELDS) if self._activity is not None else nullcontext()
            with activity:
                try:
                    prepared = fields.advance(scope, cancel_requested=cancel_requested, deadline=budget.deadline)
                except CompanySyncInterrupted:
                    return self._stopped(round_id, scope, budget, cancel_requested, monotonic)
                self.journal.finish(round_id, CompanySyncFamily.FIELDS, replace(prepared, processed_scope_complete=False, can_continue=True, deferred=True))
                self.inputs.complete_task(scope.identity, "fields", preflight_task, prepared.accepted_inputs or 0, CanonicalDigest((prepared.changed_codes, fields.plan)))
            if self._observe is not None:
                confirmed = self.journal.family(round_id, CompanySyncFamily.FIELDS)
                if confirmed is not None:
                    self._observe(confirmed)
            if budget.max_steps == 1:
                current = self.journal.family(round_id, CompanySyncFamily.FIELDS)
                return CompanySyncRoundResult(round_id, scope, (current,) if current is not None else (), False, True, "",
                    pause_reason="step_limit")
            if budget.max_steps is not None:
                budget = replace(budget, max_steps=budget.max_steps - 1)
        try:
            result = RunCompanySyncRound(self.journal, producers, scope, budget, monotonic=monotonic,
                cancel_requested=cancel_requested, observe=self._observe, activity=self._activity,
                recover_directory=announcements.recover_directory, source_ready=self._source_ready,
                sleeper=self._sleeper, transient_retry_seconds=(
                    self._document_configuration.document_transient_retry_seconds if self._document_configuration else None),
                after_resume=publish_financial_scoring if self._wall_clock is not None else None)
        except CompanySyncInterrupted:
            return self._stopped(round_id, scope, budget, cancel_requested, monotonic)
        if population_gaps:
            result = replace(result, families=tuple(replace(item, result=replace(item.result,
                gaps=item.result.gaps + tuple(gap for gap in population_gaps if gap not in item.result.gaps))) for item in result.families))
        return result

    def _ratio_inputs_closed(self, scope: CompanySyncRoundScope) -> bool:
        identity = scope.identity
        if self.inputs.pledge_day(identity) is None:
            return False
        return all(p is not None and p.accepted_pages == p.expected_pages and p.accepted_rows == p.expected_rows for p in (
            self.inputs.ratio_download_progress(identity, "pledge"), self.inputs.ratio_download_progress(identity, "unlock")))

    def _annual_inputs_settled(self, scope: CompanySyncRoundScope, check: Callable[[], None]) -> bool:
        incomes = self.inputs.annuals(scope.identity)
        if not incomes:
            return True
        with self._session(self.facts.path, check=check, read_only=True) as formal:
            return all(formal.run("""SELECT 1 FROM company_input_receipt WHERE source='eastmoney_annual_income'
                AND source_key=? AND code=? AND record_hash=? AND extraction_hash=? AND outcome='valid' LIMIT 1""",
                (row.source_key, row.code, row.record_hash, self.facts.extraction_hash)).fetchone() is not None for row in incomes)

    def _publish_financial_scoring(
        self,
        scope: CompanySyncRoundScope,
        query: StructuredCompanyQuery,
        eligible_codes: tuple[str, ...],
    ) -> None:
        if self._wall_clock is None:
            return
        published = self.normalized.publish_financial_scoring(
            query, scope.codes, scope.cutoff, eligible_codes=eligible_codes, available_at=self._wall_clock(),
        )
        if published and self._observe_financial_scoring is not None:
            self._observe_financial_scoring(scope)

    def clean_material(self, cutoff: datetime) -> None:
        """Explicit maintenance; publication never calls this operation."""
        self.material.clean_material(cutoff)

    def _read_field_population(self, codes: tuple[str, ...], check: Callable[[], None]) -> _FieldPopulation:
        with self._session(self.facts.path, check=check, read_only=True) as formal, SqliteTransaction(formal):
            return _FieldPopulation(codes, ReadCompanyFactRows(formal, codes), ReadCompanyPublicationIdentity(formal))

    def field_watermark(self, scope: CompanySyncRoundScope, eligible_codes: tuple[str, ...], check: Callable[[], None], *,
        financial_query: StructuredCompanyQuery | None = None, population: _FieldPopulation | None = None,
    ) -> str:
        check()
        eligible = frozenset(eligible_codes)
        markets = tuple(sorted({EvidenceMarket(code) for code in eligible_codes}))
        material: list[tuple[object, ...]] = []
        if population is None:
            population = self._read_field_population(eligible_codes, check)
        facts, publication = population.facts, population.publication
        unlock_open = eligible.difference(fact.code for fact in facts if fact.unlock_state is CompanyDataState.COMPLETE)
        retained_unlock_codes: set[str] = set()
        financial_proofs = self._financial_revision_proofs
        identities = tuple(sorted({identity for fact in facts for event in fact.risk_events + fact.positive_events for identity in event.evidence_ids}
            | {review.evidence_id for fact in facts for review in fact.review_reasons if review.evidence_id is not None}
            | {proof.original.identity for proof in financial_proofs if proof.original.codes[0] in eligible}))
        with self._session(self.material.path, check=check, read_only=True) as raw, SqliteTransaction(raw):
            coverage = tuple(tuple(row) for row in raw.run(
                "SELECT market,start_date,end_date FROM coverage "
                f"WHERE market IN ({','.join('?' for _ in markets)}) ORDER BY market,start_date,end_date",
                markets,
            )) if markets else ()
            for offset in range(0, len(eligible_codes), 128):
                check()
                codes = eligible_codes[offset:offset + 128]
                for row in raw.run("""SELECT c.code,d.source,d.source_key,d.record_hash,d.body_hash,d.body_checked,d.body_retained,d.title,
                    CASE WHEN d.source='cninfo' AND d.body_checked=1 AND d.body_retained=1 THEN
                      EXISTS(SELECT 1 FROM document_extractions x WHERE x.disclosure_id=d.id AND x.record_hash=d.record_hash)
                      AND NOT EXISTS(SELECT 1 FROM disclosure_codes m WHERE m.disclosure_id=d.id AND m.code<>c.code)
                    ELSE 0 END
                    FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id
                    JOIN company_document_requirements required ON required.disclosure_id=d.id
                    WHERE d.kind='announcement' AND d.published_at>=? AND d.published_at<=?
                    AND c.code IN (""" + ",".join("?" for _ in codes) + ") ORDER BY c.code,d.source,d.source_key",
                    ((scope.cutoff.date() - timedelta(days=365)).isoformat(), scope.cutoff.isoformat(), *codes)):
                    material.append((row[0], row[1], row[2], row[3], row[4].hex() if isinstance(row[4], bytes) else None, row[5], row[6]))
                    if row[8] and row[0] in unlock_open and IsCompanyUnlockListingNotice(row[7]) and not CompanyDocumentNeedsReview(row[7]):
                        retained_unlock_codes.add(row[0])
            for offset in range(0, len(identities), 512):
                check()
                batch = identities[offset:offset + 512]
                for row in raw.run("SELECT c.code,d.source,d.source_key,d.record_hash,d.body_hash,d.body_checked,d.body_retained FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE d.source_key IN (" + ",".join("?" for _ in batch) + ") ORDER BY c.code,d.source,d.source_key", batch):
                    if row[0] in eligible:
                        material.append((row[0], row[1], row[2], row[3], row[4].hex() if isinstance(row[4], bytes) else None, row[5], row[6]))
        inputs: tuple[object, ...] = (self.inputs.field_input_identity(scope.identity), coverage,
            tuple(sorted(set(material), key=lambda row: (str(row[0]), str(row[1]), str(row[2])))),
            publication.content_hash.hex() if publication is not None else None)
        if retained_unlock_codes:
            unlock_observations = self._unlock_observations(scope, frozenset(retained_unlock_codes))
            if unlock_observations:
                inputs += (unlock_observations,)
        if financial_proofs:
            query = financial_query or ResolveCompanyFinancialScoringQuery(scope.cutoff)
            inputs += (
                self.normalized.financial_observation_watermark(query, scope.codes, scope.cutoff),
                tuple((proof.original, proof.report_date, proof.checked_from, proof.checked_through,
                    proof.source_record_hash, proof.authority_evidence_hash.hex()) for proof in financial_proofs),
            )
        return CanonicalDigest(inputs)

    def _unlock_observations(self, scope: CompanySyncRoundScope, codes: frozenset[str]) -> tuple[CompanyRatio, ...]:
        start = scope.cutoff.date()
        end = start + timedelta(days=min(180, self._event_policy.unlock_days))
        return tuple(
            ratio for ratio in self.inputs.ratios(scope.identity, "unlock")
            if ratio.code in codes and ratio.ratio_pct > 0 and start <= ratio.day <= end
        )

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

    def prepare_local(self, scope: CompanySyncRoundScope, *, check: Callable[[], None], cancel_requested: Callable[[], bool],
        eligible_codes: tuple[str, ...], financial_query: StructuredCompanyQuery, population: _FieldPopulation | None = None,
    ) -> CompanySyncStepResult:
        check()
        if not eligible_codes:
            return CompanySyncStepResult(True, False, pending_count=None, gaps=("population_members_became_ineligible",))
        rules_hash = CompanyFieldProofRulesHash()
        recovered = self.inputs.pending_recovered(scope.identity)
        annuals = self.inputs.annuals(scope.identity)
        unlock_by_code: dict[str, list[CompanyRatio]] = defaultdict(list)
        observations: tuple[CompanyFinancialSourceObservation, ...] = ()
        eligible = frozenset(eligible_codes)
        for ratio in self._unlock_observations(scope, eligible):
            unlock_by_code[ratio.code].append(ratio)
        if self._financial_revision_proofs:
            observations = self.normalized.latest_financial_observations(financial_query, scope.codes, scope.cutoff)
        with (
            self._session(self.material.path, check=check, read_only=True) as raw,
            self._session(self.facts.path, check=check, read_only=True) as formal,
            SqliteTransaction(raw), SqliteTransaction(formal),
        ):
            marker = formal.run("SELECT rules_hash FROM company_field_proof_rule WHERE singleton=1").fetchone()
            revalidate = marker is None or marker[0] != rules_hash
            all_codes = tuple(sorted(set(eligible_codes) | {row[0] for row in formal.run("SELECT code FROM company_facts ORDER BY code")})) if revalidate else eligible_codes
            if (population is not None and population.codes == all_codes and population.publication is not None
                and population.publication == ReadCompanyPublicationIdentity(formal)):
                old = {fact.code: fact for fact in population.facts}
            else:
                old = {fact.code: fact for fact in ReadCompanyFactRows(formal, all_codes)}
            fields_by_code: dict[str, list[CompanyFieldConclusion]] = defaultdict(list)
            for field in ReadCompanyFieldResults(formal, all_codes):
                fields_by_code[field.code].append(field)
            links_by_code: dict[str, list[CompanyPublishedEvidenceLink]] = defaultdict(list)
            for link in ReadCompanyDocumentLinks(formal, all_codes):
                links_by_code[link.code].append(link)
            admissions: dict[str, CompanyFinancialAdmission] = {}
            if observations:
                for admission in PrepareCompanyFinancialAdmissions(raw,
                    tuple(item for item in observations if item.financial.code in eligible), scope.cutoff,
                    read_document=self._read_document, check=check, revision_proofs=self._financial_revision_proofs):
                    admissions[admission.report.code] = admission
            announcement_coverage = ReadCompanyAnnouncementCoverage(raw, eligible_codes, scope.cutoff, check)
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
                proof = admissions.get(code)
                admitted = ApplyCompanyFinancialAdmission(fact, proof, scope.cutoff) if proof is not None else fact
                item = PrepareCompanyIssuerInputs(raw, formal, admitted, cutoff=scope.cutoff,
                    extraction_hash=self.facts.extraction_hash, event_policy=self._event_policy,
                    read_document=self._read_document, check=check, revalidate=revalidate or needs_recovery_reproof, correct_measurements=code in eligible,
                    include_new_announcements=True, published_links=tuple(links_by_code.get(code, ())), recovered=recovered,
                    annual_incomes=annuals, unlock_observations=tuple(unlock_by_code.get(code, ())),
                    field_rules_hash=rules_hash, announcement_coverage=announcement_coverage.get(code))
                if proof is not None and admitted != fact:
                    receipts = CompanyFinancialAdmissionReceipts(proof, extraction_hash=self.facts.extraction_hash, published_at=scope.cutoff)
                    item = replace(item, receipts=(*item.receipts, *receipts))
                prepared.append(item)
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
    def __init__(self, runtime: CompanySyncRuntime, monotonic: Callable[[], float], eligible_codes: tuple[str, ...],
        query: StructuredCompanyQuery,
    ) -> None:
        self._runtime, self._monotonic = runtime, monotonic
        self._eligible_codes = eligible_codes
        self._financial_query = query
        self._population: _FieldPopulation | None = None
        self._plan = CompanySyncPhasePlan(CompanySyncFamily.FIELDS, CanonicalDigest(("current_field_proof", CompanyFieldProofRulesHash().hex())), refresh_on_input_change=True)

    @property
    def plan(self) -> CompanySyncPhasePlan:
        return self._plan

    def state(self, scope: CompanySyncRoundScope) -> CompanySyncPhaseState:
        check = self._runtime._io_budget.check if self._runtime._io_budget is not None else lambda: None
        self._population = None
        population = self._runtime._read_field_population(self._eligible_codes, check)
        watermark = self._runtime.field_watermark(scope, self._eligible_codes, check, financial_query=self._financial_query, population=population)
        self._population = population
        return CompanySyncPhaseState(CanonicalDigest((scope, self._plan, watermark)), False)

    def advance(self, scope: CompanySyncRoundScope, *, cancel_requested: Callable[[], bool], deadline: float) -> CompanySyncStepResult:
        def check() -> None:
            if cancel_requested() or self._monotonic() >= deadline:
                raise CompanySyncInterrupted("company_sync_local_preparation_interrupted")
        population, self._population = self._population, None
        return self._runtime.prepare_local(scope, check=check, cancel_requested=cancel_requested, eligible_codes=self._eligible_codes,
            financial_query=self._financial_query, population=population)


def _unavailable(code: str, cutoff: datetime) -> CompanyFacts:
    return CompanyFacts(code, None, CompanyDataState.UNAVAILABLE, None, CompanyDataState.UNAVAILABLE, (), None, None,
        CompanyDataState.UNAVAILABLE, (), (), (), 0, None, None, CompanyDataState.UNAVAILABLE, cutoff, cutoff, latest_financial=None)


__all__ = ["CompanySyncRuntime"]
