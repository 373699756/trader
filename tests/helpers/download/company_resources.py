"""Explicit construction inputs for isolated company data resources."""

from __future__ import annotations

from trader.download.domain.company_field_results import CompanyFieldConclusion
from trader.download.domain.company_document_links import CompanyPublishedEvidenceLink

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict

from tests.helpers.download.configuration import CompanyEventPolicyFixture, CompanySyncConfigurationFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_evidence import (
    CompanyDocumentLimits, CompanyDocumentPolicies, CompanyListing, CompanySyncConfiguration,
)
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_document_ocr import CompanyOcrEngine
from trader.download.infra.company_document_parser import CompanyTextCapability
from trader.download.infra.company_material_index import SQLiteCompanyMaterialIndex
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_protocol import DecodeCompanyLabel, EVIDENCE_SOURCES, EncodeCompanyTime
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash
from trader.download.infra.company_document_link_rows import ReadCompanyDocumentLinks
from trader.download.infra.company_sync_preparation import PrepareCompanyIssuerInputs
from trader.download.infra.company_field_recovery import RegisterCompanyMaterialDependencies, RestoreCompanyMaterialDependencies
from trader.infra.sqlite_connection import SqliteTransaction
from trader.download.domain.company_facts import CompanyFacts, CompileCompanyFacts
from trader.download.domain.company_evidence import CompanyDocument, CompanyEvidenceInput, CompanyFinancial, CompanyRatio, StructuredCompanyQuery, StructuredCompanyPage
from trader.infra.sqlite_connection import SqliteSession
from datetime import timedelta
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_http import CompanyRequestPacer
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.infra.http_contracts import HttpTransport
from trader.infra.sqlite_policy import SqliteIoPolicy


class _DocumentOptions(TypedDict, total=False):
    document_policies: CompanyDocumentPolicies
    text_capability: CompanyTextCapability


class CompanyResearchFixture(SQLiteCompanyMaterialIndex):
    """Explicit local test inputs; no source scheduler, default replenishment or repair loop."""

    def __init__(self, path: Path, *, facts_path: Path, documents: CompanyDocumentFiles,
                 event_policy: CompanyEventPolicy | None = None, sqlite_policy: SqliteIoPolicy | None = None,
                 cancel_requested: Callable[[], bool] | None = None,
                 listings: tuple[CompanyListing, ...] = (), raw_files: CompanyRawFiles | None = None) -> None:
        policy = event_policy if event_policy is not None else CompanyEventPolicyFixture()
        io = sqlite_policy if sqlite_policy is not None else SqlitePolicyFixture()
        writer = SQLiteCompanyFactsIndex(facts_path, event_policy=policy, sqlite_policy=io, cancel_requested=cancel_requested)
        writer.initialize()
        super().__init__(path, facts=writer, documents=documents, event_policy=policy, sqlite_policy=io,
                         raw_files=raw_files, cancel_requested=cancel_requested)
        self._test_financials: tuple[CompanyFinancial, ...] = ()
        self._test_ratios: tuple[CompanyRatio, ...] = ()
        self._ratio_component = "pledge"
        self._listings = listings

    def initialize(self) -> None:
        super().initialize()
        with SqliteSession(self.path, sqlite_policy=self._sqlite_policy) as raw:
            raw.run_many("INSERT OR REPLACE INTO company_population VALUES(?)",
                         ((item.code,) for item in self._listings))
            with SqliteSession(self.facts_path, sqlite_policy=self._sqlite_policy) as formal:
                RestoreCompanyMaterialDependencies(raw, formal)

    def commit_structured(self, query: StructuredCompanyQuery, page: StructuredCompanyPage, observed_at: datetime) -> int:
        self._test_financials = tuple({(item.code, item.report_date): item for item in (*self._test_financials, *page.financials)}.values())
        self._ratio_component = page.component
        self._test_ratios = tuple({(item.code, item.day): item for item in (*self._test_ratios, *page.ratios)}.values())
        return len(page.financials)+len(page.ratios)

    def _local_documents(self) -> tuple[tuple[CompanyDocument, ...], tuple[CompanyInputReceipt, ...]]:
        documents = []
        receipts = []
        with SqliteSession(self.path, sqlite_policy=self._sqlite_policy) as raw:
            for row in raw.run("SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id ORDER BY d.id,c.code"):
                item = DecodeCompanyDisclosure(row)
                text = self._documents.read(row['body_hash']) if row['body_retained'] and row['body_hash'] else ''
                documents.append(CompanyDocument(item, text, bool(row['body_checked'])))
                if row['body_checked'] and row['body_hash']:
                    receipts.append(CompanyInputReceipt(item.source, item.identity, item.codes[0], item.digest,
                        self._facts.extraction_hash, row['body_hash'], item.published_at))
        return tuple(documents), tuple(receipts)

    def _publish_prepared(self, facts: tuple[CompanyFacts, ...], changed: tuple[CompanyFacts, ...],
                          observed_at: datetime, *, revalidate_codes: frozenset[str] = frozenset(),
                          accepted_codes: frozenset[str]) -> None:
        with SqliteSession(self.path,sqlite_policy=self._sqlite_policy) as collected:
            source_pairs = tuple((row[0],row[1]) for row in collected.run("SELECT source_key,source FROM disclosures"))
            accepted = tuple(CompanyInputReceipt(row['source'],row['source_key'],row['code'],row['record_hash'],
                self._facts.extraction_hash,row['body_hash'],datetime.fromisoformat(row['published_at']))
                for row in collected.run("SELECT d.*,c.code FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE body_checked=1 AND body_hash IS NOT NULL")
                if row['code'] in accepted_codes)
        fields: list[CompanyFieldConclusion] = []
        attempts: list[CompanyInputReceipt] = []
        links: list[CompanyPublishedEvidenceLink] = []
        with SqliteSession(self.path, sqlite_policy=self._sqlite_policy) as raw, SqliteSession(
            self.facts_path, sqlite_policy=self._sqlite_policy,
        ) as formal, SqliteTransaction(formal) as provisional:
            parent = ReadCompanyPublicationIdentity(formal)
            previous_links = ReadCompanyDocumentLinks(formal, tuple(fact.code for fact in facts))
            sources = {row[0]: DecodeCompanyLabel(EVIDENCE_SOURCES, row[1]) for row in formal.run("SELECT source_key,source FROM company_evidence_ref")}
            # This is a rolled-back proof input view, never a second committed writer.
            formal.run_many("INSERT OR IGNORE INTO company_input_receipt VALUES(?,?,?,?,?,?,?,?,?)", (
                (item.source,item.source_key,item.code,item.record_hash,item.extraction_hash,item.body_hash,
                 EncodeCompanyTime(item.published_at),item.outcome,item.policy_id) for item in accepted))
            for fact in facts:
                def check() -> None:
                    if self._cancel_requested is not None and self._cancel_requested():
                        raise InterruptedError("company_sync_cancelled")

                prepared = PrepareCompanyIssuerInputs(raw, formal, fact, cutoff=observed_at,
                    extraction_hash=self._facts.extraction_hash, event_policy=self._event_policy,
                    read_document=self._documents.read, revalidate=fact.code in revalidate_codes, check=check,
                    include_new_announcements=False, correct_measurements=False, published_links=previous_links)
                fields.extend(prepared.fields)
                attempts.extend(prepared.receipts)
                links.extend(prepared.links)
            provisional.rollback()
        sources.update(source_pairs)
        self._facts.publish(changed, sources, parent, observed_at, receipts=(*accepted,*attempts),
                            field_results=tuple(fields), field_rules_hash=CompanyFieldProofRulesHash(), document_links=tuple(links))
        with SqliteSession(self.path, sqlite_policy=self._sqlite_policy) as raw, SqliteTransaction(raw, immediate=True), SqliteSession(self.facts_path, sqlite_policy=self._sqlite_policy) as formal:
            RegisterCompanyMaterialDependencies(raw, formal, facts, self._facts.extraction_hash)

    def prepare_and_publish(self, codes: tuple[str, ...], observed_at: datetime) -> None:
        with SqliteSession(self.facts_path, sqlite_policy=self._sqlite_policy) as formal:
            all_codes = tuple(row[0] for row in formal.run("SELECT code FROM company_facts ORDER BY code"))
            facts = ReadCompanyFactRows(formal, all_codes)
        if not facts:
            self.publish(codes, observed_at)
        else:
            self._publish_prepared(facts, (), observed_at, accepted_codes=frozenset(codes))

    def publish(self, codes: tuple[str, ...], observed_at: datetime) -> int:
        documents, _receipts = self._local_documents()
        with SqliteSession(self.facts_path, sqlite_policy=self._sqlite_policy) as formal:
            all_codes = tuple(row[0] for row in formal.run("SELECT code FROM company_facts ORDER BY code"))
            previous = {fact.code:fact for fact in ReadCompanyFactRows(formal,all_codes)}
        candidates = tuple(CompileCompanyFacts(CompanyEvidenceInput(code, observed_at, observed_at.date()-timedelta(days=365),
            observed_at.date(), self._facts.extraction_hash.hex(), tuple(item for item in documents if code in item.disclosure.codes and (item.checked or item.disclosure.published_at.date() >= observed_at.date()-timedelta(days=365))),
            True, tuple(item for item in self._test_financials if item.code == code),
            next((item for item in self._test_ratios if item.code == code and self._ratio_component == 'pledge'), None),
            tuple(item for item in self._test_ratios if item.code == code and self._ratio_component == 'unlock'), False, (), False, ()),
            self._event_policy, previous=previous.get(code)) for code in codes)
        changed = tuple(fact for fact in candidates if fact != previous.get(fact.code))
        previous.update((fact.code,fact) for fact in candidates)
        facts = tuple(previous[code] for code in sorted(previous))
        self._publish_prepared(facts, changed, observed_at, revalidate_codes=frozenset(fact.code for fact in changed), accepted_codes=frozenset(codes))
        return len(changed)


class SQLiteCompanyFactsIndexFixture(SQLiteCompanyFactsIndex):
    def __init__(
        self,
        path: Path,
        *,
        event_policy: CompanyEventPolicy | None = None,
        sqlite_policy: SqliteIoPolicy | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(
            path, event_policy=event_policy if event_policy is not None else CompanyEventPolicyFixture(),
            sqlite_policy=sqlite_policy if sqlite_policy is not None else SqlitePolicyFixture(),
            cancel_requested=cancel_requested,
        )


class SQLiteCompanyEvidenceReaderFixture(SQLiteCompanyEvidenceReader):
    def __init__(
        self,
        path: Path,
        *,
        event_policy: CompanyEventPolicy | None = None,
        sqlite_policy: SqliteIoPolicy | None = None,
    ) -> None:
        super().__init__(
            path, event_policy=event_policy if event_policy is not None else CompanyEventPolicyFixture(),
            sqlite_policy=sqlite_policy if sqlite_policy is not None else SqlitePolicyFixture(),
        )


class CompanyDocumentFilesFixture(CompanyDocumentFiles):
    def __init__(
        self,
        root: Path,
        *,
        configuration: CompanySyncConfiguration | None = None,
        max_text_characters: int | None = None,
        max_bytes: int | None = None,
        reserve_bytes: int | None = None,
    ) -> None:
        settings = configuration
        if settings is None and (max_text_characters is None or max_bytes is None or reserve_bytes is None):
            settings = CompanySyncConfigurationFixture()
        if max_text_characters is None:
            assert settings is not None
            max_text_characters = settings.extended_document_limits.max_text_characters
        if max_bytes is None:
            assert settings is not None
            max_bytes = settings.temporary_text_capacity_bytes
        if reserve_bytes is None:
            assert settings is not None
            reserve_bytes = settings.temporary_disk_reserve_bytes
        super().__init__(root, max_text_characters=max_text_characters, max_bytes=max_bytes, reserve_bytes=reserve_bytes)


class EastmoneyCompanySourceFixture(EastmoneyCompanySource):
    def __init__(
        self,
        session: HttpTransport,
        *,
        monotonic: Callable[[], float],
        configuration: CompanySyncConfiguration | None = None,
        timeout_seconds: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        if timeout_seconds is None:
            settings = configuration if configuration is not None else CompanySyncConfigurationFixture()
            timeout_seconds = settings.source_timeout_seconds
        super().__init__(session, monotonic=monotonic, timeout_seconds=timeout_seconds, cancel_requested=cancel_requested)


class CninfoMarketDisclosureSourceFixture(CninfoMarketDisclosureSource):
    def __init__(
        self,
        session: HttpTransport,
        *,
        monotonic: Callable[[], float],
        sleeper: Callable[[float], None],
        configuration: CompanySyncConfiguration | None = None,
        timeout_seconds: float | None = None,
        document_download_timeout_seconds: float | None = None,
        document_parse_timeout_seconds: float | None = None,
        request_interval_seconds: float | None = None,
        document_limits: CompanyDocumentLimits | None = None,
        extended_document_limits: CompanyDocumentLimits | None = None,
        document_policies: CompanyDocumentPolicies | None = None,
        text_capability: CompanyTextCapability | None = None,
        ocr_engine: CompanyOcrEngine | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        raw_files: CompanyRawFiles | None = None,
        pacer: CompanyRequestPacer | None = None,
        text_reserve_bytes: int | None = None,
        ocr_reserve_bytes: int | None = None,
    ) -> None:
        settings = configuration
        needs_settings = any(value is None for value in (
            timeout_seconds, document_download_timeout_seconds, document_parse_timeout_seconds,
            request_interval_seconds, document_limits, extended_document_limits,
            text_reserve_bytes, ocr_reserve_bytes, raw_files,
        ))
        if settings is None and needs_settings:
            settings = CompanySyncConfigurationFixture()
        self._temporary_raw: TemporaryDirectory[str] | None = None
        if raw_files is None:
            assert settings is not None
            self._temporary_raw = TemporaryDirectory(prefix="test-company-raw-")
            raw_files = CompanyRawFiles(
                Path(self._temporary_raw.name), capacity=settings.raw_capacity_bytes,
                analysis_reserve=settings.raw_analysis_reserve_bytes, free_reserve=settings.temporary_disk_reserve_bytes,
                maximum_document_bytes=(extended_document_limits or settings.extended_document_limits).max_bytes,
            )
        if settings is not None:
            timeout_seconds = settings.source_timeout_seconds if timeout_seconds is None else timeout_seconds
            document_download_timeout_seconds = settings.document_download_timeout_seconds if document_download_timeout_seconds is None else document_download_timeout_seconds
            document_parse_timeout_seconds = settings.document_parse_timeout_seconds if document_parse_timeout_seconds is None else document_parse_timeout_seconds
            request_interval_seconds = settings.request_interval_seconds if request_interval_seconds is None else request_interval_seconds
            document_limits = settings.document_limits if document_limits is None else document_limits
            extended_document_limits = settings.extended_document_limits if extended_document_limits is None else extended_document_limits
            text_reserve_bytes = settings.text_task_reserve_bytes if text_reserve_bytes is None else text_reserve_bytes
            ocr_reserve_bytes = settings.ocr_task_reserve_bytes if ocr_reserve_bytes is None else ocr_reserve_bytes
        assert timeout_seconds is not None and document_download_timeout_seconds is not None
        assert document_parse_timeout_seconds is not None and request_interval_seconds is not None
        assert document_limits is not None and extended_document_limits is not None
        options: _DocumentOptions = {}
        if document_policies is not None:
            options["document_policies"] = document_policies
        if text_capability is not None:
            options["text_capability"] = text_capability
        super().__init__(
            session, monotonic=monotonic, sleeper=sleeper, timeout_seconds=timeout_seconds,
            document_download_timeout_seconds=document_download_timeout_seconds,
            document_parse_timeout_seconds=document_parse_timeout_seconds, request_interval_seconds=request_interval_seconds,
            document_limits=document_limits, extended_document_limits=extended_document_limits,
            ocr_engine=ocr_engine, cancel_requested=cancel_requested, raw_files=raw_files, pacer=pacer,
            text_reserve_bytes=text_reserve_bytes, ocr_reserve_bytes=ocr_reserve_bytes, **options,
        )
