"""Research identity rules, independent of persistent JSON representation."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from trader.infra.serialization.canonical_codec import CanonicalExclusion, canonical_digest, canonical_text
from trader.training.domain.evaluation.specification import HISTORICAL_RESEARCH_SPEC

RESEARCH_IDENTITY_EXCLUSION = CanonicalExclusion(
    "research_identity", HISTORICAL_RESEARCH_SPEC.research_identity, "exclude_from_legacy_hash"
)


def canonical_research_hash(value: object) -> str:
    return canonical_digest(value, exclusion=RESEARCH_IDENTITY_EXCLUSION)


def research_rows_hash(rows: Iterable[object]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_text(row, exclusion=RESEARCH_IDENTITY_EXCLUSION).encode())
        digest.update(b"\n")
    return digest.hexdigest()


__all__ = ["RESEARCH_IDENTITY_EXCLUSION", "canonical_research_hash", "research_rows_hash"]
