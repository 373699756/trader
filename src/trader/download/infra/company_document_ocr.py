"""Bounded local OCR for scanned company announcement PDFs."""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from trader.download.domain.company_evidence import CompanyDocumentFailure
from trader.infra.http_contracts import HttpCancelled

_MAX_RENDERED_BYTES = 256 * 1024 * 1024
_MAX_TSV_BYTES = 8 * 1024 * 1024
_MIN_CONFIDENCE = 35.0


@dataclass(frozen=True)
class CompanyOcrCapability:
    identity: str
    tesseract: str | None
    pdftoppm: str | None
    languages: str
    failure: str | None = None

    @property
    def available(self) -> bool:
        return self.failure is None


def DetectCompanyOcr(languages: str) -> CompanyOcrCapability:
    tesseract = shutil.which("tesseract")
    pdftoppm = shutil.which("pdftoppm")
    if tesseract is None or pdftoppm is None:
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
    return CompanyOcrCapability(identity, tesseract, pdftoppm, languages)


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
        if not self.capability.available:
            raise CompanyDocumentFailure(self.capability.failure or "disclosure_document_ocr_unavailable", payload_hash=payload_hash)
        with self._lock:
            self._check_cancelled()
            deadline = self._monotonic() + self._timeout_seconds
            with tempfile.TemporaryDirectory(prefix="trader-company-ocr-") as directory:
                root = Path(directory)
                pdf = root / "document.pdf"
                pdf.write_bytes(payload)
                prefix = root / "page"
                self._run(
                    (
                        self.capability.pdftoppm or "pdftoppm",
                        "-png",
                        "-r",
                        str(self._dpi),
                        "-f",
                        "1",
                        "-l",
                        str(self._max_pages + 1),
                        str(pdf),
                        str(prefix),
                    ),
                    deadline,
                    payload_hash,
                    "disclosure_document_ocr_unreadable",
                )
                images = tuple(sorted(root.glob("page-*.png"), key=_page_number))
                if len(images) > self._max_pages:
                    raise CompanyDocumentFailure("disclosure_document_ocr_page_limit", payload_hash=payload_hash)
                if not images or sum(image.stat().st_size for image in images) > _MAX_RENDERED_BYTES:
                    raise CompanyDocumentFailure("disclosure_document_ocr_resource_limit", payload_hash=payload_hash)
                texts: list[str] = []
                confidences: list[float] = []
                for image in images:
                    output = self._run(
                        (
                            self.capability.tesseract or "tesseract",
                            str(image),
                            "stdout",
                            "-l",
                            self.capability.languages,
                            "--psm",
                            "6",
                            "tsv",
                        ),
                        deadline,
                        payload_hash,
                        "disclosure_document_ocr_unreadable",
                    )
                    if len(output) > _MAX_TSV_BYTES:
                        raise CompanyDocumentFailure("disclosure_document_ocr_resource_limit", payload_hash=payload_hash)
                    text, confidence = _read_tsv(output)
                    texts.append(text)
                    confidences.extend(confidence)
            result = "\n".join(texts).strip()
            quality = sum(confidences) / len(confidences) if confidences else 0.0
            if len("".join(result.split())) < 80 or quality < _MIN_CONFIDENCE:
                raise CompanyDocumentFailure("disclosure_document_ocr_low_quality", payload_hash=payload_hash)
            return result, quality

    def _run(
        self,
        command: tuple[str, ...],
        deadline: float,
        payload_hash: bytes,
        failure: str,
    ) -> bytes:
        process: subprocess.Popen[bytes] = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        try:
            while True:
                self._check_cancelled()
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise CompanyDocumentFailure("disclosure_document_ocr_timeout", payload_hash=payload_hash)
                try:
                    output, _ = process.communicate(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise CompanyDocumentFailure(failure, payload_hash=payload_hash)
            return output
        finally:
            _close_process(process)

    def _check_cancelled(self) -> None:
        if self._cancel_requested is not None and self._cancel_requested():
            raise HttpCancelled("http_cancelled")


def _page_number(path: Path) -> int:
    try:
        return int(path.stem.rsplit("-", 1)[1])
    except (IndexError, ValueError) as exc:
        raise ValueError("company_ocr_page_identity_invalid") from exc


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
        if confidence < 0:
            continue
        words.append(columns[11].strip())
        confidences.append(confidence)
    return " ".join(words), confidences


def _close_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=1)


__all__ = ["CompanyOcrCapability", "CompanyOcrEngine", "DetectCompanyOcr"]
