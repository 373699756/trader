"""Run real isolated PDF extraction against bounded synthetic documents."""

import io
import subprocess
import sys

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from trader.download.domain.company_evidence import CompanyDocumentLimits
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from tests.unit.infra.test_company_sources import NOW, Session
from trader.download.domain.company_evidence import Disclosure
from trader.infra.http_contracts import HttpResponse


def _pdf(pages=1, *, encrypted=False, blank=False):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=500, height=500)
        if not blank:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 10 10 Td (" + b"Official announcement evidence. " * 5 + b") Tj ET")
            page[NameObject("/Contents")] = stream
    if encrypted:
        writer.encrypt("fixture")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _parse(payload, *, max_pages=200, max_text=1_000_000):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "trader.download.infra.company_document_parser",
            "--max-bytes",
            "16777216",
            "--max-pages",
            str(max_pages),
            "--max-text-characters",
            str(max_text),
        ],
        input=payload,
        capture_output=True,
        timeout=15,
        check=False,
    )


def test_long_text_pdf_requires_full_extraction_and_respects_configured_page_limit():
    payload = _pdf(31)
    result = _parse(payload)
    assert result.returncode == 0 and result.stdout.count(b"Official announcement evidence.") == 31 * 5
    refused = _parse(payload, max_pages=30)
    assert refused.returncode == 4 and not refused.stdout


@pytest.mark.parametrize("payload,code", [(_pdf(encrypted=True), 3), (_pdf(blank=True), 7), (b"broken PDF", 2)])
def test_pdf_failure_categories_do_not_emit_partial_verified_text(payload, code):
    result = _parse(payload)
    assert result.returncode == code and not result.stdout


def test_extracted_text_limit_rejects_instead_of_truncating():
    result = _parse(_pdf(), max_text=80)
    assert result.returncode == 6 and not result.stdout


def test_real_parser_reason_and_config_limits_reach_the_source():
    class PdfSession(Session):
        def get(self, url, **kwargs):
            self.requests.append(kwargs)
            return HttpResponse(200, _pdf(2))

    session = PdfSession({})
    source = CninfoMarketDisclosureSource(
        session,
        monotonic=lambda: 0,
        sleeper=lambda _: None,
        document_limits=CompanyDocumentLimits(max_bytes=65536, max_pages=1),
    )
    item = Disclosure(
        "cninfo", "fixture", ("600001",), "风险公告", NOW, "https://static.cninfo.com.cn/finalpage/a.PDF", "a"
    )
    with pytest.raises(ValueError, match="disclosure_document_page_limit"):
        source.document(item, deadline=30)
    assert session.requests[0]["max_response_bytes"] == 65536
