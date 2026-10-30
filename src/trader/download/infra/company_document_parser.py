"""Isolated, bounded Poppler PDF text extraction."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from trader.download.domain.company_evidence import CompanyDocumentLimits


@dataclass(frozen=True)
class CompanyTextCapability:
    identity: str
    pdftotext: str | None
    pdfinfo: str | None
    failure: str | None = None

    @property
    def available(self) -> bool:
        return self.failure is None


def DetectCompanyText() -> CompanyTextCapability:
    pdftotext = shutil.which("pdftotext")
    pdfinfo = shutil.which("pdfinfo")
    if pdftotext is None or pdfinfo is None:
        return CompanyTextCapability(
            "unavailable", pdftotext, pdfinfo, "disclosure_document_text_unavailable"
        )
    try:
        text_version = subprocess.run((pdftotext, "-v"), capture_output=True, timeout=5, check=False)
        info_version = subprocess.run((pdfinfo, "-v"), capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return CompanyTextCapability(
            "unavailable", pdftotext, pdfinfo, "disclosure_document_text_unavailable"
        )
    if text_version.returncode or info_version.returncode:
        return CompanyTextCapability(
            "unavailable", pdftotext, pdfinfo, "disclosure_document_text_unavailable"
        )
    identity = hashlib.sha256(
        text_version.stdout + text_version.stderr + info_version.stdout + info_version.stderr
    ).hexdigest()[:20]
    return CompanyTextCapability(identity, pdftotext, pdfinfo)


def Main() -> int:
    signal.signal(signal.SIGTERM, _terminate)
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--max-bytes", required=True, type=int)
    parser.add_argument("--max-pages", required=True, type=int)
    parser.add_argument("--max-text-characters", required=True, type=int)
    parser.add_argument("--pdftotext", default="pdftotext")
    parser.add_argument("--pdfinfo", default="pdfinfo")
    args = parser.parse_args()
    limits = CompanyDocumentLimits(args.max_bytes, args.max_pages, args.max_text_characters)
    payload = sys.stdin.buffer.read(limits.max_bytes + 1)
    if len(payload) > limits.max_bytes:
        return 2
    try:
        # The supervising caller owns cleanup after the complete process family exits.
        root = Path(args.directory)
        pdf = root / "document.pdf"
        text_path = root / "document.txt"
        pdf.write_bytes(payload)
        details = subprocess.run((args.pdfinfo, str(pdf)), capture_output=True, check=False)
        metadata = (details.stdout + details.stderr).decode("utf-8", errors="replace")
        if _metadata_value(metadata, "Encrypted").lower().startswith("yes") or "Incorrect password" in metadata:
            return 3
        if details.returncode:
            return 2
        pages = _metadata_value(metadata, "Pages")
        if not pages.isdigit():
            return 2
        if int(pages) > limits.max_pages:
            return 4
        extracted = subprocess.run(
            (args.pdftotext, "-enc", "UTF-8", "-nopgbrk", str(pdf), str(text_path)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if extracted.returncode or not text_path.exists():
            return 2
        maximum_bytes = limits.max_text_characters * 4 + 1
        with text_path.open("rb") as stream:
            encoded = stream.read(maximum_bytes)
        if len(encoded) >= maximum_bytes:
            return 6
        text = encoded.decode("utf-8", errors="replace").strip()
        if len(text) > limits.max_text_characters:
            return 6
        if len(text) < 80:
            return 7 if not text else 8
        sys.stdout.buffer.write(text.encode("utf-8"))
        return 0
    except (OSError, ValueError):
        return 2


def _metadata_value(metadata: str, name: str) -> str:
    prefix = f"{name}:"
    return next(
        (line.split(":", 1)[1].strip() for line in metadata.splitlines() if line.startswith(prefix)),
        "",
    )


def _terminate(_signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(143)


if __name__ == "__main__":
    raise SystemExit(Main())


__all__ = ["CompanyTextCapability", "DetectCompanyText", "Main"]
