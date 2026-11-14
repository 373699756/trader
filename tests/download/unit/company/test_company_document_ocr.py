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


def _extract(engine, payload, root):
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload).digest()
    pdf = root / (digest.hex() + ".pdf")
    pdf.write_bytes(payload)
    return engine.extract(pdf, digest)


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
    engine = CompanyOcrEngine(capability, timeout_seconds=2, page_timeout_seconds=2, max_pages=2, dpi=150)
    payload = _pdf()
    text, quality = _extract(engine, payload, tmp_path)
    assert text == "verified" * 12 and quality == 92

    weak = _executable(
        tmp_path / "weak-tesseract",
        "print('level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\twidth\\theight\\tconf\\ttext')\n"
        "print('5\\t1\\t1\\t1\\t1\\t1\\t0\\t0\\t1\\t1\\t10\\tshort')\n",
    )
    engine = CompanyOcrEngine(
        CompanyOcrCapability("weak", weak, renderer, "chi_sim+eng"),
        timeout_seconds=2,
        page_timeout_seconds=2,
        max_pages=2,
        dpi=150,
    )
    with pytest.raises(CompanyDocumentFailure, match="disclosure_document_ocr_low_quality"):
        _extract(engine, payload, tmp_path)


def test_unavailable_ocr_fails_without_starting_a_process(tmp_path):
    engine = CompanyOcrEngine(
        CompanyOcrCapability(
            "unavailable", None, None, "chi_sim+eng", "disclosure_document_ocr_unavailable"
        ),
        timeout_seconds=1,
        page_timeout_seconds=1,
        max_pages=1,
        dpi=150,
    )
    payload = _pdf()
    with pytest.raises(CompanyDocumentFailure, match="disclosure_document_ocr_unavailable"):
        _extract(engine, payload, tmp_path)


@pytest.mark.parametrize("retry_each_page", (False, True))
def test_low_quality_page_uses_one_layout_retry_and_records_failure_metrics(monkeypatch, tmp_path, retry_each_page):
    engine = CompanyOcrEngine(
        CompanyOcrCapability("fixture", "tesseract", "pdftoppm", "eng"),
        timeout_seconds=2, page_timeout_seconds=2, max_pages=2, dpi=150,
        retry_each_page=retry_each_page,
    )
    modes = []

    def run(command, *args, **kwargs):
        if command[0] == "pdfinfo":
            return b"Pages: 2\nPage 1 size: 200 x 200 pts\nPage 2 size: 200 x 200 pts\n"
        return b"image"

    def recognize(image, deadline, digest, mode):
        modes.append(mode)
        return ("short", [10.0]) if mode == 6 else ("recovered " * 12, [92.0])

    monkeypatch.setattr(engine, "_run", run)
    monkeypatch.setattr(engine, "_recognize", recognize)
    payload = _pdf(2)
    text, quality = _extract(engine, payload, tmp_path)
    assert "recovered" in text and quality == (92 if retry_each_page else 51)
    assert modes == ([6, 6, 6, 6, 1, 6, 6, 6, 6, 1] if retry_each_page else [6, 3, 6])

    monkeypatch.setattr(engine, "_recognize", lambda *args: ("short", [10.0]))
    assert _extract(engine, payload, tmp_path) == (text, quality)
    with pytest.raises(CompanyDocumentFailure) as caught:
        _extract(engine, payload, tmp_path / "new-input")
    diagnostic = caught.value.diagnostic
    assert diagnostic.phase == "quality" and diagnostic.pages == 2
    assert diagnostic.characters == 10 and diagnostic.confidence == 10


@pytest.mark.parametrize("recovery", ("rotation", "resolution"))
def test_each_sideways_page_is_rotated_and_successful_pages_are_reused(monkeypatch, tmp_path, recovery):
    from pypdf import PdfReader

    engine = CompanyOcrEngine(
        CompanyOcrCapability("fixture", "tesseract", "pdftoppm", "eng"),
        timeout_seconds=10, page_timeout_seconds=10, max_pages=2, dpi=150, retry_each_page=True,
    )
    rotations = []

    def run(command, *args, **kwargs):
        if command[0] == "pdfinfo":
            return b"Pages: 2\nPage 1 size: 200 x 200 pts\nPage 2 size: 200 x 200 pts\n"
        if command[-1].endswith("oriented.pdf"):
            angle = PdfReader(command[-1]).pages[0].rotation
            rotations.append(angle)
            return f"{angle}:{command[command.index('-r') + 1]}".encode()
        return b"sideways"

    def recognize(image, *args):
        expected = b"90:150" if recovery == "rotation" else b"0:300"
        return ("verified " * 12, [90.0]) if image.read_bytes() == expected else ("short", [10.0])

    monkeypatch.setattr(engine, "_run", run)
    monkeypatch.setattr(engine, "_recognize", recognize)
    payload = _pdf(2)
    assert _extract(engine, payload, tmp_path)[1] == 90.0
    expected_rotations = [0, 90, 0, 90] if recovery == "rotation" else [0, 0]
    assert rotations == expected_rotations
    assert _extract(engine, payload, tmp_path)[1] == 90.0
    assert rotations == expected_rotations
    assert not tuple(tmp_path.rglob("oriented.pdf"))


def test_timeout_records_render_phase_and_page(monkeypatch, tmp_path):
    engine = CompanyOcrEngine(
        CompanyOcrCapability("fixture", "tesseract", "pdftoppm", "eng"),
        timeout_seconds=2, page_timeout_seconds=2, max_pages=2, dpi=150,
    )

    def run(command, *args, **kwargs):
        if command[0] == "pdfinfo":
            return b"Pages: 1\nPage 1 size: 200 x 200 pts\n"
        raise CompanyDocumentFailure("disclosure_document_ocr_timeout")

    monkeypatch.setattr(engine, "_run", run)
    with pytest.raises(CompanyDocumentFailure) as caught:
        _extract(engine, b"pdf", tmp_path)
    assert caught.value.diagnostic.phase == "render"
    assert caught.value.diagnostic.page == caught.value.diagnostic.pages == 1


@pytest.mark.parametrize("recovered", [True, False])
def test_resolution_timeout_leaves_budget_for_rotated_pages(monkeypatch, tmp_path, recovered):
    clock = [0.0]
    attempts = []
    engine = CompanyOcrEngine(
        CompanyOcrCapability("fixture", "tesseract", "pdftoppm", "eng"),
        timeout_seconds=120,
        page_timeout_seconds=120,
        max_pages=1,
        dpi=150,
        retry_each_page=True,
        monotonic=lambda: clock[0],
    )

    def run(command, *args, **kwargs):
        if command[0] == "pdfinfo":
            return b"Pages: 1\nPage 1 size: 200 x 200 pts\n"
        return b"rotated" if command[-1].endswith("oriented.pdf") else b"original"

    def recognize(image, deadline, digest, mode):
        attempts.append((mode, deadline))
        if image.read_bytes() == b"original" and mode == 6:
            return "weak " * 30, [10.0]
        if len(attempts) == 2 or not recovered:
            clock[0] = deadline
            raise CompanyDocumentFailure("disclosure_document_ocr_timeout", payload_hash=digest)
        return "verified " * 20, [90.0]

    monkeypatch.setattr(engine, "_run", run)
    monkeypatch.setattr(engine, "_recognize", recognize)
    payload = _pdf()
    if recovered:
        assert _extract(engine, payload, tmp_path)[1] == 90.0
        assert attempts == [(6, 120), (6, 60), (6, 80)]
        assert _extract(engine, payload, tmp_path)[1] == 90.0
        assert len(attempts) == 3, "completed page checkpoints must remain reusable"
    else:
        with pytest.raises(CompanyDocumentFailure, match="disclosure_document_ocr_timeout"):
            _extract(engine, payload, tmp_path)
        assert attempts == [(6, 120), (6, 60), (6, 80), (6, 100), (1, 120)]
        assert not tuple(tmp_path.rglob("1.json")), "timed out pages must not be checkpointed as completed"
    assert not tuple(tmp_path.rglob("page.png"))
    assert not tuple(tmp_path.rglob("oriented.pdf"))


def test_weak_checkpoint_is_retried_while_successful_result_remains_reusable(monkeypatch, tmp_path):
    engine = CompanyOcrEngine(
        CompanyOcrCapability("fixture", "tesseract", "pdftoppm", "eng"),
        timeout_seconds=120, page_timeout_seconds=120, max_pages=1, dpi=150, retry_each_page=True,
    )
    calls = []
    strong = [False]

    def run(command, *args, **kwargs):
        return b"Pages: 1\nPage 1 size: 200 x 200 pts\n" if command[0] == "pdfinfo" else b"image"

    def recognize(*args):
        calls.append(1)
        return ("verified " * 20, [90.0]) if strong[0] else ("weak " * 20, [10.0])

    monkeypatch.setattr(engine, "_run", run)
    monkeypatch.setattr(engine, "_recognize", recognize)
    payload = _pdf()
    with pytest.raises(CompanyDocumentFailure, match="disclosure_document_ocr_low_quality"):
        _extract(engine, payload, tmp_path)
    assert tuple(tmp_path.rglob("1.json"))
    strong[0] = True
    before = len(calls)
    assert _extract(engine, payload, tmp_path)[1] == 90.0
    assert len(calls) == before + 1
    assert _extract(engine, payload, tmp_path)[1] == 90.0
    assert len(calls) == before + 1


def test_page_limit_is_checked_before_renderer_starts(tmp_path):
    renderer = _executable(tmp_path / "renderer", "raise AssertionError('must not render')\n")
    engine = CompanyOcrEngine(
        CompanyOcrCapability("fixture", renderer, renderer, "eng"),
        timeout_seconds=2, page_timeout_seconds=2, max_pages=1, dpi=150,
    )
    payload = _pdf(2)
    with pytest.raises(CompanyDocumentFailure, match="ocr_page_limit"):
        _extract(engine, payload, tmp_path)


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
        timeout_seconds=10, page_timeout_seconds=10, max_pages=2, dpi=150,
    )
    payload = _pdf()
    started = time.monotonic()
    with pytest.raises(CompanyDocumentFailure, match="ocr_resource_limit"):
        _extract(engine, payload, tmp_path)
    assert time.monotonic() - started < 5
    assert Path(directory_record.read_text()).exists()
    assert not tuple(tmp_path.rglob("page.png"))


def test_real_poppler_and_tesseract_extract_complete_synthetic_text(tmp_path):
    from tests.helpers.download.company.company_document_parser import _pdf as text_pdf

    capability = DetectCompanyOcr("eng")
    if not capability.available:
        pytest.skip("local Poppler/Tesseract unavailable")
    engine = CompanyOcrEngine(capability, timeout_seconds=20, page_timeout_seconds=20, max_pages=1, dpi=150)
    payload = text_pdf()
    text, quality = _extract(engine, payload, tmp_path)
    assert "Official" in text and len("".join(text.split())) >= 80 and quality >= 35
