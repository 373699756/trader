
import hashlib

import os

import select

import subprocess

import sys

import time

from threading import Event, Timer



import pytest



from tests.helpers.download.company.company_sources import NOW, Session

from trader.download.domain.company_evidence import CompanyDocumentFailure, CompanyDocumentLimits, Disclosure

from tests.helpers.shared.configuration import CninfoMarketDisclosureSourceFixture as CninfoMarketDisclosureSource

from trader.download.infra.company_document_parser import DetectCompanyText

from trader.infra.http_contracts import HttpCancelled, HttpResponse

from tests.helpers.download.company.company_document_parser import (
    _parse,
    _pdf,
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


@pytest.mark.parametrize("short,code", [(False, 0), (True, 8)])
def test_permission_encryption_does_not_block_text_or_short_text_ocr_route(short, code):
    result = _parse(_pdf(owner_only=True, short=short))
    assert result.returncode == code
    if code == 0:
        assert result.stdout.count(b"Official announcement evidence.") == 5
    else:
        assert not result.stdout


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
    source.download_document(item)
    extraction = source.extract_document(item)
    assert extraction.method == "text" and extraction.policy_id == "limits_1:default"
    assert session.requests[0]["max_response_bytes"] == 64 * 1024 * 1024
    assert len(session.requests) == 1


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
    source.download_document(item)
    with pytest.raises(CompanyDocumentFailure, match="disclosure_document_ocr_required"):
        source.extract_document(item)
    extraction = source.extract_document(item, route="ocr")
    assert extraction.method == "ocr" and extraction.quality == 88.0


@pytest.mark.skipif(os.name != "posix", reason="signal-based parser termination probe")
@pytest.mark.parametrize("cancel,ignore_term", [(True, False), (True, True), (False, False)])
def test_blocked_parser_is_cancelled_or_times_out_and_is_reaped(monkeypatch, cancel, ignore_term, tmp_path):
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
    pdf = tmp_path / (hashlib.sha256(b"fixture").hexdigest() + ".pdf")
    pdf.write_bytes(b"fixture")
    try:
        expected = HttpCancelled if cancel else CompanyDocumentFailure
        with pytest.raises(expected, match=None if cancel else "disclosure_document_parse_timeout"):
            source._parse_document(pdf)
        assert time.monotonic() - started < 5
        assert len(children) == 1 and children[0].poll() is not None
        assert children[0].stdin is None and children[0].stdout.closed
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
