"""Persistent research projection with the original historical field exclusions."""

from __future__ import annotations

from trader.infra.serialization.canonical_codec import CanonicalValue, canonical_text, canonical_value
from trader.training.domain.evaluation.research_identity import RESEARCH_IDENTITY_EXCLUSION


def research_json(value: object) -> str:
    return canonical_text(value, exclusion=RESEARCH_IDENTITY_EXCLUSION)


def research_value(value: object) -> CanonicalValue:
    return canonical_value(value, exclusion=RESEARCH_IDENTITY_EXCLUSION)


__all__ = ["research_json", "research_value"]
