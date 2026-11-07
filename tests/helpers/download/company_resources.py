"""Explicit construction inputs for isolated company data resources."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict

from tests.helpers.download.configuration import CompanyEventPolicyFixture, CompanySyncConfigurationFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_evidence import (
    CompanyDocumentLimits, CompanyDocumentPolicies, CompanyListing, CompanyPublicationProgress, CompanyStockProgress, CompanySyncConfiguration,
)
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_document_ocr import CompanyOcrEngine
from trader.download.infra.company_document_parser import CompanyTextCapability
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
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


class SQLiteCompanyEvidenceIndexFixture(SQLiteCompanyEvidenceIndex):
    def __init__(
        self,
        path: Path,
        *,
        facts_path: Path,
        documents: CompanyDocumentFiles,
        event_policy: CompanyEventPolicy | None = None,
        sqlite_policy: SqliteIoPolicy | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        stock_progress: Callable[[CompanyStockProgress | CompanyPublicationProgress], None] | None = None,
        listings: tuple[CompanyListing, ...] = (),
        raw_files: CompanyRawFiles | None = None,
        batch_publication_reads: bool = False,
    ) -> None:
        super().__init__(
            path, facts_path=facts_path, documents=documents,
            event_policy=event_policy if event_policy is not None else CompanyEventPolicyFixture(),
            sqlite_policy=sqlite_policy if sqlite_policy is not None else SqlitePolicyFixture(),
            cancel_requested=cancel_requested, stock_progress=stock_progress, listings=listings,
            raw_files=raw_files, batch_publication_reads=batch_publication_reads,
        )


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
        raw_path: Path | None = None,
    ) -> None:
        super().__init__(
            path, event_policy=event_policy if event_policy is not None else CompanyEventPolicyFixture(),
            sqlite_policy=sqlite_policy if sqlite_policy is not None else SqlitePolicyFixture(), raw_path=raw_path,
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
