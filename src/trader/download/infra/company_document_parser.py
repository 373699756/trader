"""Isolated, bounded PDF text extraction; scanned/oversized documents remain unknown."""

from __future__ import annotations

import argparse
import io
import sys

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from trader.download.domain.company_evidence import CompanyDocumentLimits


def Main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-bytes", required=True, type=int)
    parser.add_argument("--max-pages", required=True, type=int)
    parser.add_argument("--max-text-characters", required=True, type=int)
    args = parser.parse_args()
    limits = CompanyDocumentLimits(args.max_bytes, args.max_pages, args.max_text_characters)
    payload = sys.stdin.buffer.read(limits.max_bytes + 1)
    try:
        if len(payload) > limits.max_bytes:
            raise ValueError("document_size_limit")
        reader = PdfReader(io.BytesIO(payload), strict=True)
        if reader.is_encrypted:
            return 3
        if len(reader.pages) > limits.max_pages:
            return 4
        parts: list[str] = []
        for page in reader.pages:
            content = page.get_contents()
            if content is not None and len(content.get_data()) > 8 * 1024 * 1024:
                return 5
            parts.append(page.extract_text())
            if sum(map(len, parts)) > limits.max_text_characters:
                return 6
        text = "\n".join(parts).strip()
        if len(text) < 80:
            return 7 if not text else 8
        sys.stdout.buffer.write(text.encode("utf-8"))
        return 0
    except (PdfReadError, ValueError, TypeError, KeyError, RecursionError):
        return 2


if __name__ == "__main__":
    raise SystemExit(Main())


__all__ = ["Main"]
