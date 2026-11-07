"""Fresh SQLite, production use case and reader contracts without supplier writes."""

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from trader.download.application.company_evidence import SyncCompanyEvidence
from trader.download.domain.company_evidence import CompanyDocumentExtraction, CompanyDocumentPolicies, CompanyListing, CompanySyncConfiguration, Disclosure, DisclosurePage, DisclosurePartition, StructuredCompanyPage
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from tests.helpers.download.configuration import CompanyEventPolicyFixture, CompanyEvidenceRequestFixture as CompanyEvidenceRequest
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture as CompanyDocumentFiles, SQLiteCompanyEvidenceIndexFixture as SQLiteCompanyEvidenceIndex

CompanyEventPolicy = CompanyEventPolicyFixture

NOW = datetime(2026, 10, 18, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))

_TEST_COMPANY_CONFIGURATION = replace(
    LoadCompanySyncConfiguration(Path(__file__).resolve().parents[4]), document_workers=1
)

_TEST_DOCUMENT_POLICIES = CompanyDocumentPolicies("test-text", "test-ocr", "test-limits")


def _extraction(text: str) -> CompanyDocumentExtraction:
    complete = text if len(text.strip()) >= 80 else (text + "\n") * (80 // max(1, len(text)) + 2)
    return CompanyDocumentExtraction(complete, "text", hashlib.sha256(complete.encode()).digest(), "test-text")


def _sync_company_evidence(
    dependencies,
    codes,
    observed_at,
    *,
    configuration: CompanySyncConfiguration = _TEST_COMPANY_CONFIGURATION,
    news_codes=(),
):
    return SyncCompanyEvidence(
        dependencies,
        CompanyEvidenceRequest(tuple(codes), observed_at, configuration, news_codes=tuple(news_codes)),
    )


class Sources:
    source = "fixture"
    news_source = "eastmoney_news"

    def __init__(self):
        self.calls = []
        self.failure = False

    def fetch(self, first, *args, **kwargs):
        self.calls.append(first)
        if self.failure:
            raise TimeoutError("fixture")
        if isinstance(first, DisclosurePartition):
            return DisclosurePage((), 0, False)
        return StructuredCompanyPage(first.component, first.page, 1, 0)

    def document(self, item, *, route="standard"):
        raise AssertionError("ordinary announcements must not fetch a document")

    def document_ready(self, item):
        return False

    def download_document(self, item):
        return None

    def extract_document(self, item, *, route="standard"):
        return self.document(item) if route == "standard" else self.document(item, route=route)

    def split_queries(self, partition):
        raise ValueError("cninfo_query_unsplittable")

    def pledge_day(self, observed_at):
        return observed_at.date()

    def news(self, code, **kwargs):
        self.calls.append(("news", code))
        return ()


def _index(tmp_path):
    index = SQLiteCompanyEvidenceIndex(
        tmp_path / "company_evidence.sqlite3",
        facts_path=(tmp_path / "company_evidence.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "company_evidence.sqlite3").parent / "text"),
        listings=(CompanyListing("600001", NOW.date() - timedelta(days=365)),),
    )
    index.initialize()
    return index


def _disclosure(identity="first", title="普通公告", digest="a", when=NOW):
    return Disclosure("cninfo", identity, ("600001",), title, when, "", digest)
