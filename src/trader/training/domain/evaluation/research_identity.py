"""Research identity rules, independent of persistent JSON representation."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from trader.infra.serialization.canonical_codec import CanonicalDigest, CanonicalText


def CanonicalResearchHash(value: object) -> str:
    return CanonicalDigest(value)


def ResearchRowsHash(rows: Iterable[object]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(CanonicalText(row).encode())
        digest.update(b"\n")
    return digest.hexdigest()


__all__ = ["CanonicalResearchHash", "ResearchRowsHash"]
