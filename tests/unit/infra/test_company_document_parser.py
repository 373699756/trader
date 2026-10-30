"""Run real isolated PDF extraction against bounded synthetic documents."""

import io
import os
import select
import subprocess
import sys
import time
from threading import Event, Timer
from tempfile import TemporaryDirectory

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from tests.unit.infra.test_company_sources import NOW, Session
from trader.download.domain.company_evidence import CompanyDocumentFailure, CompanyDocumentLimits, Disclosure
from tests.helpers.configuration import CninfoMarketDisclosureSourceFixture as CninfoMarketDisclosureSource
from trader.download.infra.company_document_parser import DetectCompanyText
from trader.infra.http_contracts import HttpCancelled, HttpResponse


def _pdf(pages=1, *, encrypted=False, blank=False, short=False):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=2000, height=500)
        if not blank:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
            )
            stream = DecodedStreamObject()
            text = b"Short text." if short else b"Official announcement evidence. " * 5
            stream.set_data(b"BT /F1 12 Tf 10 10 Td (" + text + b") Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("fixture")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _parse(payload, *, max_pages=200, max_text=1_000_000):
    with TemporaryDirectory(prefix="test-company-text-") as directory:
        return _parse_in_directory(payload, directory, max_pages, max_text)


def _parse_in_directory(payload, directory, max_pages, max_text):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "trader.download.infra.company_document_parser",
            "--directory",
            directory,
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


def test_short_text_is_distinct_from_no_extractable_text_and_neither_is_verified():
    short = _parse(_pdf(short=True))
    blank = _parse(_pdf(blank=True))
    assert short.returncode == 8 and blank.returncode == 7
    assert not short.stdout and not blank.stdout


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
        document_limits=CompanyDocumentLimits(max_bytes=65536, max_pages=1, max_text_characters=1_000_000),
    )
    item = Disclosure(
        "cninfo", "fixture", ("600001",), "风险公告", NOW, "https://static.cninfo.com.cn/finalpage/a.PDF", "a"
    )
    extraction = source.document(item)
    assert extraction.method == "text" and extraction.policy_id == "limits_1:default"
    assert session.requests[0]["max_response_bytes"] == 65536


def test_poppler_capability_is_versioned_and_parser_timeout_falls_back_to_ocr(monkeypatch):
    capability = DetectCompanyText()
    assert capability.available and capability.identity not in {"", "unavailable"}

    class PdfSession(Session):
        def get(self, url, **kwargs):
            return HttpResponse(200, _pdf())

    class Ocr:
        def extract(self, payload, payload_hash):
            assert payload and len(payload_hash) == 32
            return "OCR recovered announcement evidence. " * 4, 88.0

    source = CninfoMarketDisclosureSource(
        PdfSession({}),
        monotonic=lambda: 0,
        sleeper=lambda _: None,
        text_capability=capability,
        ocr_engine=Ocr(),
    )
    monkeypatch.setattr(
        source,
        "_parse_document",
        lambda *_args: (_ for _ in ()).throw(CompanyDocumentFailure("disclosure_document_parse_timeout")),
    )
    item = Disclosure(
        "cninfo", "timeout", ("600001",), "风险公告", NOW, "https://static.cninfo.com.cn/finalpage/a.PDF", "a"
    )
    extraction = source.document(item)
    assert extraction.method == "ocr" and extraction.quality == 88.0


@pytest.mark.skipif(os.name != "posix", reason="signal-based parser termination probe")
@pytest.mark.parametrize("cancel,ignore_term", [(True, False), (True, True), (False, False)])
def test_blocked_parser_is_cancelled_or_times_out_and_is_reaped(monkeypatch, cancel, ignore_term):
    cancelled = Event()
    children = []
    timers = []
    popen = subprocess.Popen

    def start(_command, **kwargs):
        program = (
            "import signal,time\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
            + "print('READY', flush=True)\ntime.sleep(30)\n"
        )
        child = popen([sys.executable, "-c", program], **kwargs)
        children.append(child)
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline() == b"READY\n"
        if cancel:
            timer = Timer(0.1, cancelled.set)
            timers.append(timer)
            timer.start()
        return child

    monkeypatch.setattr("trader.download.infra.company_disclosure_source.subprocess.Popen", start)
    source = CninfoMarketDisclosureSource(
        Session({}),
        monotonic=time.monotonic,
        sleeper=time.sleep,
        document_parse_timeout_seconds=5 if cancel else 0.2,
        cancel_requested=cancelled.is_set,
    )
    started = time.monotonic()
    try:
        expected = HttpCancelled if cancel else CompanyDocumentFailure
        with pytest.raises(expected, match=None if cancel else "disclosure_document_parse_timeout"):
            source._parse_document(b"fixture")
        assert time.monotonic() - started < 5
        assert len(children) == 1 and children[0].poll() is not None
        assert children[0].stdin.closed and children[0].stdout.closed
        if ignore_term:
            assert children[0].returncode == -9
    finally:
        for timer in timers:
            timer.cancel()
            timer.join(timeout=1)
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
