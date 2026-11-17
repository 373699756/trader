
from dataclasses import replace



import pytest

from tests.helpers.download.company import company_evidence as fixtures


from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition

from trader.infra.sqlite_connection import SqliteSession

from trader.download.infra.company_facts_index import CompanyInputReceipt,ReadCompanyPublicationIdentity


from tests.helpers.download.company.company_pipeline import (
    NOW,
    _seed,
)


@pytest.mark.parametrize("stage", ["downloading", "downloaded", "analyzing", "ocr_pending"])
def test_retry_pdf_survives_old_failure_receipt_cleanup(tmp_path, stage):
    from trader.download.infra.company_raw_files import CompanyRawFiles

    index = fixtures._index(tmp_path)
    item = _seed(index, 1)[0]
    index.document_failure(item, "old-ocr-policy", "disclosure_document_ocr_low_quality", NOW, NOW)
    index.publish(item.codes, NOW)
    raw = CompanyRawFiles(tmp_path, capacity=10000000, analysis_reserve=1000000, free_reserve=1000,
                          maximum_document_bytes=64 * 1024**2)
    index._raw_files = raw
    directory = raw.directory(item)
    directory.mkdir(parents=True)
    partial = directory / "download.part"
    partial.write_bytes(b"%PDF-isolated-retry-fixture")
    local = raw.complete(item, partial)
    index.document_stage(item, stage)
    index.publish(item.codes, NOW)
    index.clean_material(NOW)
    assert local.path.exists(), "old failure receipt deleted the new input before OCR"
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT stage FROM document_tasks").require_one()[0] == stage
    index.document_failure(item, "new-ocr-policy", "disclosure_document_ocr_low_quality", NOW, NOW)
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    index._facts.publish((),{},parent,NOW,receipts=(CompanyInputReceipt(item.source,item.identity,item.codes[0],
        item.digest,index._facts.extraction_hash,None,NOW,"disclosure_document_ocr_low_quality","new-ocr-policy"),))
    index.clean_material(NOW)
    assert raw.local(item) is not None
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT stage FROM document_tasks").require_one()[0] == "cleaned"




















def test_document_revision_removes_stale_success_metadata(tmp_path):
    index = fixtures._index(tmp_path)
    item = _seed(index, 1)[0]
    assert index.commit_document(item, fixtures._extraction("原公告完整正文"), NOW)
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM document_extractions").require_one()[0] == 1
    revised = replace(item, digest="revised")
    index.commit_page(
        DisclosurePartition("sse", NOW.date(), NOW.date()), DisclosurePage((revised,), 1, False), NOW
    )
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM document_extractions").require_one()[0] == 0
