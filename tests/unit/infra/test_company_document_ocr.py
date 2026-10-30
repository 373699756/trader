"""Bounded OCR capability and subprocess contracts."""

import io
import hashlib
import subprocess
import time
from pathlib import Path

import pytest
from pypdf import PdfWriter

from trader.download.domain.company_evidence import CompanyDocumentFailure
from trader.download.infra.company_document_ocr import (
    CompanyOcrCapability,
    CompanyOcrEngine,
    DetectCompanyOcr,
)


def _pdf(pages=1) -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    writer.write(output)
    return output.getvalue()


def _executable(path: Path, body: str) -> str:
    path.write_text(f"#!/usr/bin/env python3\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_capability_requires_binaries_and_requested_languages(monkeypatch):
    monkeypatch.setattr("trader.download.infra.company_document_ocr.shutil.which", lambda _name: None)
    assert DetectCompanyOcr("chi_sim+eng").failure == "disclosure_document_ocr_unavailable"

    monkeypatch.setattr("trader.download.infra.company_document_ocr.shutil.which", lambda name: f"/fake/{name}")

    def run(command, **_kwargs):
        if command[1] == "--version":
            return subprocess.CompletedProcess(command, 0, b"tesseract 5", b"")
        if command[1] == "--list-langs":
            return subprocess.CompletedProcess(command, 0, b"List of available languages (1):\neng\n", b"")
        return subprocess.CompletedProcess(command, 0, b"", b"pdftoppm 1")

    monkeypatch.setattr("trader.download.infra.company_document_ocr.subprocess.run", run)
    assert DetectCompanyOcr("chi_sim+eng").failure == "disclosure_document_ocr_language_unavailable"
    assert DetectCompanyOcr("eng").available


def test_ocr_renders_one_page_and_rejects_low_quality(tmp_path):
    renderer = _executable(
        tmp_path / "pdftoppm",
        "import sys\nsys.stdout.buffer.write(b'image')\n",
    )
    recognizer = _executable(
        tmp_path / "tesseract",
        "print('level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\twidth\\theight\\tconf\\ttext')\n"
        "print('5\\t1\\t1\\t1\\t1\\t1\\t0\\t0\\t1\\t1\\t92\\t' + 'verified' * 12)\n",
    )
    capability = CompanyOcrCapability("fixture", recognizer, renderer, "chi_sim+eng")
    engine = CompanyOcrEngine(capability, timeout_seconds=2, max_pages=2, dpi=150)
    payload = _pdf()
    text, quality = engine.extract(payload, hashlib.sha256(payload).digest())
    assert text == "verified" * 12 and quality == 92

    weak = _executable(
        tmp_path / "weak-tesseract",
        "print('level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\twidth\\theight\\tconf\\ttext')\n"
        "print('5\\t1\\t1\\t1\\t1\\t1\\t0\\t0\\t1\\t1\\t10\\tshort')\n",
    )
    engine = CompanyOcrEngine(
        CompanyOcrCapability("weak", weak, renderer, "chi_sim+eng"),
        timeout_seconds=2,
        max_pages=2,
        dpi=150,
    )
    with pytest.raises(CompanyDocumentFailure, match="disclosure_document_ocr_low_quality"):
        engine.extract(payload, hashlib.sha256(payload).digest())


def test_unavailable_ocr_fails_without_starting_a_process():
    engine = CompanyOcrEngine(
        CompanyOcrCapability(
            "unavailable", None, None, "chi_sim+eng", "disclosure_document_ocr_unavailable"
        ),
        timeout_seconds=1,
        max_pages=1,
        dpi=150,
    )
    payload = _pdf()
    with pytest.raises(CompanyDocumentFailure, match="disclosure_document_ocr_unavailable"):
        engine.extract(payload, hashlib.sha256(payload).digest())


def test_page_limit_is_checked_before_renderer_starts(tmp_path):
    renderer = _executable(tmp_path / "renderer", "raise AssertionError('must not render')\n")
    engine = CompanyOcrEngine(
        CompanyOcrCapability("fixture", renderer, renderer, "eng"),
        timeout_seconds=2, max_pages=1, dpi=150,
    )
    payload = _pdf(2)
    with pytest.raises(CompanyDocumentFailure, match="ocr_page_limit"):
        engine.extract(payload, hashlib.sha256(payload).digest())


@pytest.mark.parametrize("oversized", ["image", "tsv"])
def test_oversized_output_is_stopped_while_tool_is_running(tmp_path, monkeypatch, oversized):
    from trader.download.infra import company_document_ocr as ocr

    directory_record = tmp_path / "directory"
    flood = "import os,time\nos.write(1,b'x'*(9*1024*1024))\ntime.sleep(30)\n"
    renderer = _executable(
        tmp_path / "renderer",
        "import pathlib,sys\n"
        f"pathlib.Path({str(directory_record)!r}).write_text(str(pathlib.Path(sys.argv[-1]).parent))\n"
        + (flood if oversized == "image" else "sys.stdout.buffer.write(b'image')\n"),
    )
    recognizer = _executable(tmp_path / "recognizer", flood)
    monkeypatch.setattr(ocr, "_MAX_RENDERED_BYTES", 1024)
    engine = CompanyOcrEngine(
        CompanyOcrCapability("fixture", recognizer, renderer, "eng"),
        timeout_seconds=10, max_pages=2, dpi=150,
    )
    payload = _pdf()
    started = time.monotonic()
    with pytest.raises(CompanyDocumentFailure, match="ocr_resource_limit"):
        engine.extract(payload, hashlib.sha256(payload).digest())
    assert time.monotonic() - started < 5
    assert not Path(directory_record.read_text()).exists()


def test_real_poppler_and_tesseract_extract_complete_synthetic_text():
    from tests.unit.infra.test_company_document_parser import _pdf as text_pdf

    capability = DetectCompanyOcr("eng")
    if not capability.available:
        pytest.skip("local Poppler/Tesseract unavailable")
    engine = CompanyOcrEngine(capability, timeout_seconds=20, max_pages=1, dpi=150)
    payload = text_pdf()
    text, quality = engine.extract(payload, hashlib.sha256(payload).digest())
    assert "Official" in text and len("".join(text.split())) >= 80 and quality >= 35
