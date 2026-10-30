"""Bounded local OCR for scanned company announcement PDFs."""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from trader.download.domain.company_evidence import CompanyDocumentFailure, CompanyOcrDiagnostic
from trader.download.infra.company_document_process import CaptureDocumentTool
from trader.infra.http_contracts import HttpCancelled

_MAX_RENDERED_BYTES = 256 * 1024 * 1024
_MAX_TSV_BYTES = 8 * 1024 * 1024
_MIN_CONFIDENCE = 35.0
_MAX_IMAGE_EDGE = 4096
_MAX_TEXT_CHARACTERS = 2_000_000


@dataclass(frozen=True)
class CompanyOcrCapability:
    identity: str
    tesseract: str | None
    pdftoppm: str | None
    languages: str
    failure: str | None = None
    pdfinfo: str | None = "pdfinfo"

    @property
    def available(self) -> bool:
        return self.failure is None


def DetectCompanyOcr(languages: str) -> CompanyOcrCapability:
    tesseract = shutil.which("tesseract")
    pdftoppm = shutil.which("pdftoppm")
    pdfinfo = shutil.which("pdfinfo")
    if tesseract is None or pdftoppm is None or pdfinfo is None:
        return CompanyOcrCapability("unavailable", tesseract, pdftoppm, languages, "disclosure_document_ocr_unavailable")
    try:
        version = subprocess.run(
            (tesseract, "--version"), capture_output=True, timeout=5, check=False
        )
        listed = subprocess.run(
            (tesseract, "--list-langs"), capture_output=True, timeout=5, check=False
        )
        renderer = subprocess.run(
            (pdftoppm, "-v"), capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return CompanyOcrCapability("unavailable", tesseract, pdftoppm, languages, "disclosure_document_ocr_unavailable")
    installed = {
        line.strip() for line in listed.stdout.decode("utf-8", errors="replace").splitlines()[1:] if line.strip()
    }
    if listed.returncode or any(language not in installed for language in languages.split("+")):
        return CompanyOcrCapability(
            "language-unavailable",
            tesseract,
            pdftoppm,
            languages,
            "disclosure_document_ocr_language_unavailable",
        )
    identity = hashlib.sha256(version.stdout + listed.stdout + renderer.stderr).hexdigest()[:20]
    return CompanyOcrCapability(identity, tesseract, pdftoppm, languages, pdfinfo=pdfinfo)


class CompanyOcrEngine:
    """Serialize OCR and own every renderer/recognizer process it starts."""

    def __init__(
        self,
        capability: CompanyOcrCapability,
        *,
        timeout_seconds: float,
        max_pages: int,
        dpi: int,
        cancel_requested: Callable[[], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capability = capability
        self._timeout_seconds = timeout_seconds
        self._max_pages = max_pages
        self._dpi = dpi
        self._cancel_requested = cancel_requested
        self._monotonic = monotonic
        self._lock = Lock()

    def extract(self, payload: bytes, payload_hash: bytes) -> tuple[str, float]:
        diagnostic = CompanyOcrDiagnostic("metadata", 0, 0, 0, None, 6)
        if not self.capability.available:
            raise CompanyDocumentFailure(self.capability.failure or "disclosure_document_ocr_unavailable", payload_hash=payload_hash)
        while not self._lock.acquire(timeout=0.1):
            self._check_cancelled()
        try:
            self._check_cancelled()
            deadline = self._monotonic() + self._timeout_seconds
            with tempfile.TemporaryDirectory(prefix="trader-company-ocr-") as directory:
                root = Path(directory)
                pdf = root / "document.pdf"
                pdf.write_bytes(payload)
                metadata = self._run(
                    (self.capability.pdfinfo or "pdfinfo", "-f", "1", "-l", str(self._max_pages + 1), str(pdf)),
                    deadline,
                    payload_hash,
                    "disclosure_document_ocr_unreadable",
                    maximum_bytes=65536,
                )
                pages = next((line.split(":", 1)[1].strip() for line in metadata.decode("utf-8", errors="replace").splitlines() if line.startswith("Pages:")), "")
                if not pages.isdigit() or int(pages) < 1:
                    raise CompanyDocumentFailure("disclosure_document_ocr_unreadable", payload_hash=payload_hash)
                diagnostic = CompanyOcrDiagnostic("metadata", 0, int(pages), 0, None, 6)
                if int(pages) > self._max_pages:
                    raise CompanyDocumentFailure("disclosure_document_ocr_page_limit", payload_hash=payload_hash)
                edges = _render_edges(metadata, int(pages), self._dpi, payload_hash)
                texts: list[str] = []
                confidence_sum = 0.0
                confidence_count = characters = rendered_bytes = 0
                fallback_used = False
                image = root / "page.png"
                for page, edge in enumerate(edges, start=1):
                    diagnostic = CompanyOcrDiagnostic("render", page, int(pages), characters, None, 6)
                    rendered = self._run(
                        (
                            self.capability.pdftoppm or "pdftoppm", "-png", "-singlefile",
                            "-r", str(self._dpi), "-scale-to", str(edge),
                            "-f", str(page), "-l", str(page), str(pdf),
                        ),
                        deadline,
                        payload_hash,
                        "disclosure_document_ocr_unreadable",
                        maximum_bytes=_MAX_RENDERED_BYTES - rendered_bytes,
                    )
                    if not rendered:
                        raise CompanyDocumentFailure("disclosure_document_ocr_unreadable", payload_hash=payload_hash)
                    rendered_bytes += len(rendered)
                    image.write_bytes(rendered)
                    del rendered
                    diagnostic = CompanyOcrDiagnostic("recognition", page, int(pages), characters, None, 6)
                    text, confidence = self._recognize(image, deadline, payload_hash, 6)
                    page_characters = len("".join(text.split()))
                    quality = sum(confidence) / len(confidence) if confidence else 0.0
                    if not fallback_used and (page_characters < 80 or quality < _MIN_CONFIDENCE):
                        fallback_used = True
                        diagnostic = CompanyOcrDiagnostic("recognition", page, int(pages), page_characters, quality, 3)
                        alternative, alternative_confidence = self._recognize(image, deadline, payload_hash, 3)
                        alternative_characters = len("".join(alternative.split()))
                        alternative_quality = (
                            sum(alternative_confidence) / len(alternative_confidence) if alternative_confidence else 0.0
                        )
                        if (
                            alternative_characters >= 80 and alternative_quality >= _MIN_CONFIDENCE
                            or alternative_characters >= page_characters and alternative_quality >= quality
                        ):
                            text, confidence = alternative, alternative_confidence
                        else:
                            diagnostic = CompanyOcrDiagnostic("recognition", page, int(pages), page_characters, quality, 6)
                    image.unlink()
                    characters += len(text) + 1
                    if characters > _MAX_TEXT_CHARACTERS:
                        raise CompanyDocumentFailure("disclosure_document_ocr_resource_limit", payload_hash=payload_hash)
                    texts.append(text)
                    confidence_sum += sum(confidence)
                    confidence_count += len(confidence)
            result = "\n".join(texts).strip()
            quality = confidence_sum / confidence_count if confidence_count else 0.0
            diagnostic = CompanyOcrDiagnostic(
                "quality", int(pages), int(pages), len("".join(result.split())), quality, diagnostic.segmentation_mode
            )
            if len("".join(result.split())) < 80 or quality < _MIN_CONFIDENCE:
                raise CompanyDocumentFailure("disclosure_document_ocr_low_quality", payload_hash=payload_hash)
            return result, quality
        except CompanyDocumentFailure as exc:
            raise CompanyDocumentFailure(exc.reason, payload_hash=payload_hash, diagnostic=diagnostic) from exc
        finally:
            self._lock.release()

    def _recognize(
        self, image: Path, deadline: float, payload_hash: bytes, mode: int
    ) -> tuple[str, list[float]]:
        output = self._run(
            (self.capability.tesseract or "tesseract", str(image), "stdout", "-l", self.capability.languages,
             "--psm", str(mode), "tsv"),
            deadline,
            payload_hash,
            "disclosure_document_ocr_unreadable",
        )
        return _read_tsv(output)

    def _run(
        self,
        command: tuple[str, ...],
        deadline: float,
        payload_hash: bytes,
        failure: str,
        *,
        maximum_bytes: int = _MAX_TSV_BYTES,
    ) -> bytes:
        def check() -> None:
            self._check_cancelled()
            if self._monotonic() >= deadline:
                raise CompanyDocumentFailure("disclosure_document_ocr_timeout", payload_hash=payload_hash)

        try:
            code, output = CaptureDocumentTool(command, maximum_bytes=maximum_bytes, check=check)
        except OverflowError as exc:
            raise CompanyDocumentFailure("disclosure_document_ocr_resource_limit", payload_hash=payload_hash) from exc
        if code:
            raise CompanyDocumentFailure(failure, payload_hash=payload_hash)
        return output

    def _check_cancelled(self) -> None:
        if self._cancel_requested is not None and self._cancel_requested():
            raise HttpCancelled("http_cancelled")


def _render_edges(metadata: bytes, pages: int, dpi: int, payload_hash: bytes) -> tuple[int, ...]:
    sizes = re.findall(
        r"Page\s+(\d+)\s+size:\s+([0-9.eE+-]+)\s+x\s+([0-9.eE+-]+)\s+pts",
        metadata.decode("utf-8", errors="replace"),
    )
    if tuple(int(page) for page, _, _ in sizes) != tuple(range(1, pages + 1)):
        raise CompanyDocumentFailure("disclosure_document_ocr_unreadable", payload_hash=payload_hash)
    edges = []
    for _, width, height in sizes:
        dimensions = (float(width), float(height))
        if any(not math.isfinite(value) or value <= 0 for value in dimensions):
            raise CompanyDocumentFailure("disclosure_document_ocr_resource_limit", payload_hash=payload_hash)
        # Cap before multiplication, keeping huge PDF dimensions finite.
        edge = math.ceil(min(_MAX_IMAGE_EDGE * 72 / dpi, max(dimensions)) * dpi / 72)
        edges.append(min(_MAX_IMAGE_EDGE, max(1, edge)))
    return tuple(edges)


def _read_tsv(payload: bytes) -> tuple[str, list[float]]:
    words: list[str] = []
    confidences: list[float] = []
    for line in payload.decode("utf-8", errors="replace").splitlines()[1:]:
        columns = line.split("\t", 11)
        if len(columns) != 12 or not columns[11].strip():
            continue
        try:
            confidence = float(columns[10])
        except ValueError:
            continue
        if not math.isfinite(confidence) or not 0 <= confidence <= 100:
            continue
        words.append(columns[11].strip())
        confidences.append(confidence)
    return " ".join(words), confidences


__all__ = ["CompanyOcrCapability", "CompanyOcrEngine", "DetectCompanyOcr"]
