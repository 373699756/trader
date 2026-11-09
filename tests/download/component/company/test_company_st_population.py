"""Current ST qualification also applies to saved company work and fill runs."""

from dataclasses import replace

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader.download.application.company_evidence import CompanyEvidenceDependencies, SyncCompanyEvidence
from trader.download.domain.company_evidence import (
    BuildCompanyDocumentPolicies,
    CompanyEvidenceRequest,
    DisclosurePage,
)
from trader.infra.sqlite_connection import SqliteSession


@pytest.mark.parametrize("fill_missing", (False, True))
def test_saved_unknown_documents_resume_while_known_st_waits(tmp_path, fill_missing):
    index = fixtures._index(tmp_path)
    now = fixtures.NOW
    codes = ("600001", "600002", "600003")
    index.prepare_publication(codes[:2], now)
    part = next(part for part in index.partitions(("sse",), now) if part.start == now.date())
    items = tuple(
        replace(
            fixtures._disclosure(code, "关于收到立案告知书的公告"),
            codes=(code,),
            digest=code,
        )
        for code in codes
    )
    index.commit_page(part, DisclosurePage(items, len(items), False), now)
    first = PreparedFixture(now).official_universe[0]
    reference = PreparedFixture(now, tuple(replace(first, code=code) for code in codes))
    reference = replace(
        reference,
        st_evidence=tuple(
            replace(item, status=status)
            for item, status in zip(reference.st_evidence, ("ever_st", "unknown", "clear"), strict=True)
        ),
    )
    stages = []

    class Source(fixtures.Sources):
        def document_ready(self, item):
            stages.append(("ready", item.codes))
            assert item.codes in (("600002",), ("600003",))
            return True

        def download_document(self, item):
            stages.append(("download", item.codes))
            assert item.codes in (("600002",), ("600003",))

        def extract_document(self, item, *, route="standard"):
            stages.append((route, item.codes))
            assert item.codes in (("600002",), ("600003",))
            return fixtures._extraction("本公司收到证监会立案告知书。")

    source = Source()
    request = CompanyEvidenceRequest.from_reference(
        reference,
        now,
        fixtures._TEST_COMPANY_CONFIGURATION,
        CompanyEventPolicyFixture(),
        news_codes=codes,
        fill_missing=fill_missing,
    )
    assert request.codes == ("600002", "600003")
    result = SyncCompanyEvidence(CompanyEvidenceDependencies(index, source, source, lambda: 0.0), request)

    assert result.requested_codes == result.published_codes == 2
    assert ("standard", ("600002",)) in stages
    assert ("standard", ("600003",)) in stages
    assert [call for call in source.calls if isinstance(call, tuple) and call[0] == "news"] == (
        [] if fill_missing else [("news", "600002"), ("news", "600003")]
    )
    with SqliteSession(index.path) as connection:
        rows = connection.run(
            "SELECT c.code,d.body_checked FROM disclosures d "
            "JOIN disclosure_codes c ON c.disclosure_id=d.id ORDER BY c.code"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("600001", 0), ("600002", 1), ("600003", 1)]
    assert reference.st_evidence[1].status == "unknown"
    policies = BuildCompanyDocumentPolicies(fixtures._TEST_COMPANY_CONFIGURATION, "unavailable")
    assert index.pending_documents(request.codes, now, policies=policies) == ()
