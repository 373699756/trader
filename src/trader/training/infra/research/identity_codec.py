"""Persistent research projection with the original historical field exclusions."""

from __future__ import annotations

from trader.infra.serialization.canonical_codec import CanonicalText, CanonicalValue, EncodeCanonicalValue
from trader.training.domain.evaluation.research_identity import RESEARCH_IDENTITY_EXCLUSION


def ResearchJson(value: object) -> str:
    return CanonicalText(value, exclusion=RESEARCH_IDENTITY_EXCLUSION)


def ResearchValue(value: object) -> CanonicalValue:
    return EncodeCanonicalValue(value, exclusion=RESEARCH_IDENTITY_EXCLUSION)


__all__ = ["ResearchJson", "ResearchValue"]
