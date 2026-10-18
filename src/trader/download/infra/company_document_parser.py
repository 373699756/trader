"""Isolated, bounded PDF text extraction; scanned/oversized documents remain unknown."""

from __future__ import annotations

import io
import sys

from pypdf import PdfReader
from pypdf.errors import PdfReadError


def Main() -> int:
    payload = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    try:
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("document_size_limit")
        reader = PdfReader(io.BytesIO(payload), strict=True)
        if reader.is_encrypted or len(reader.pages) > 30:
            return 2
        parts: list[str] = []
        for page in reader.pages:
            content = page.get_contents()
            if content is not None and len(content.get_data()) > 8 * 1024 * 1024:
                return 2
            parts.append(page.extract_text())
            if sum(map(len, parts)) > 200_000:
                return 2
        text = "\n".join(parts).strip()
        if len(text) < 80:
            return 2
        sys.stdout.buffer.write(text.encode("utf-8"))
        return 0
    except (PdfReadError, ValueError, TypeError, KeyError, RecursionError):
        return 2


if __name__ == "__main__":
    raise SystemExit(Main())


__all__ = ["Main"]
